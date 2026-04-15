"""Stage 3: Ontology alignment + tagging - rules A1-A5."""

from __future__ import annotations

import logging
import re
import uuid

from semcore.core.context import PipelineContext
from semcore.pipeline.base import Stage
from semcore.providers.base import RelationalStore

log = logging.getLogger(__name__)

_SEMANTIC_ROLE_TAGS = {
    "definition": "定义", "mechanism": "机制", "constraint": "约束",
    "config": "配置", "fault": "故障", "troubleshooting": "排障",
    "best_practice": "最佳实践", "performance": "性能",
    "comparison": "对比", "table": "表格", "code": "配置",
}

# Context signal patterns loaded from ontology/patterns/context_signals.yaml
# (no hardcoded patterns — loaded at runtime via OntologyRegistry)

_LAYER_TAG_TYPE = {
    "concept":   "canonical",
    "mechanism": "mechanism_tag",
    "method":    "method_tag",
    "condition": "condition_tag",
    "scenario":  "scenario_tag",
}


class AlignStage(Stage):
    name = "align"

    def __init__(self) -> None:
        self._ontology = None
        self._store: RelationalStore | None = None
        self._llm = None

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._ontology = app.ontology
        self._store = app.store
        self._llm = getattr(app, "llm", None)
        self._context_patterns = getattr(app.ontology, "context_signal_patterns", [])
        self._crawler_store = getattr(app, "crawler_store", None) or app.store
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        self._run(source_doc_id)
        return ctx

    def _run(self, source_doc_id: str) -> None:
        """Align all segments for a document; insert segment_tags."""
        store = self._store
        segments = store.fetchall(
            "SELECT * FROM segments WHERE source_doc_id=%s AND lifecycle_state='active'",
            (source_doc_id,),
        )
        log.info("Align start doc=%s segments=%d", source_doc_id, len(segments))
        total_tags = 0
        pending = 0
        candidate_terms = 0
        pending_seg_ids: list[str] = []  # segments that got 0 canonical tags

        for seg in segments:
            tags, candidates = self.align_segment(seg)
            saved = self._save_tags(seg["segment_id"], tags)
            total_tags += saved
            candidate_terms += candidates
            canonical_count = sum(1 for t in tags if t["tag_type"] == "canonical")
            log.debug(
                "  seg=%s type=%s tags=%d canonical=%d candidates=%d",
                str(seg["segment_id"])[:12], seg.get("segment_type", "?"),
                saved, canonical_count, candidates,
            )

            if canonical_count == 0:
                store.execute(
                    "UPDATE segments SET lifecycle_state='pending_alignment' WHERE segment_id=%s",
                    (seg["segment_id"],),
                )
                pending += 1
                pending_seg_ids.append(str(seg["segment_id"]))

        # RST propagation: for segments with 0 canonical tags, borrow tags from RST neighbors
        propagated = 0
        if pending_seg_ids:
            propagated = self._propagate_via_rst(source_doc_id, pending_seg_ids)

        self._crawler_store.execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) "
            "VALUES ('relation_extraction',%s,'pending','0.2.0')",
            (source_doc_id,),
        )
        log.info(
            "Aligned doc=%s tags=%d pending_segments=%d rst_propagated=%d candidates_seen=%d",
            source_doc_id,
            total_tags,
            pending,
            propagated,
            candidate_terms,
        )

    def align_segment(self, segment: dict) -> tuple[list[dict], int]:
        """Rules A1-A5: produce canonical + semantic_role + context tags."""
        # Use normalized_text (lowercase) for alias matching
        text = segment.get("normalized_text") or segment.get("raw_text", "")
        # Use raw_text (preserves case) for candidate term discovery
        raw_text = segment.get("raw_text") or text
        tags: list[dict] = []
        ontology = self._ontology

        matched_nodes: dict[str, float] = {}
        for surface, node_id, conf in self._find_terms(text):
            if node_id not in matched_nodes or matched_nodes[node_id] < conf:
                matched_nodes[node_id] = conf

        for node_id, conf in matched_nodes.items():
            node = ontology.get_node_dict(node_id)
            layer = node.get("knowledge_layer", "concept") if node else "concept"
            tag_type = _LAYER_TAG_TYPE.get(layer, "canonical")
            tags.append({
                "tag_type":        tag_type,
                "tag_value":       node["canonical_name"] if node else node_id,
                "ontology_node_id": node_id,
                "confidence":      conf,
                "tagger":          "rule",
            })

        # Embedding fallback: if no canonical tags from exact match, try semantic matching
        canonical_count = sum(1 for n, c in matched_nodes.items()
                             if _LAYER_TAG_TYPE.get(
                                 (ontology.get_node_dict(n) or {}).get("knowledge_layer", "concept"),
                                 "canonical") == "canonical")
        if canonical_count == 0:
            for node_id, conf in self._embedding_match(text):
                if node_id not in matched_nodes:
                    matched_nodes[node_id] = conf
                    node = ontology.get_node_dict(node_id)
                    layer = node.get("knowledge_layer", "concept") if node else "concept"
                    tag_type = _LAYER_TAG_TYPE.get(layer, "canonical")
                    tags.append({
                        "tag_type":        tag_type,
                        "tag_value":       node["canonical_name"] if node else node_id,
                        "ontology_node_id": node_id,
                        "confidence":      conf,
                        "tagger":          "embedding",
                    })

        candidate_terms = self._collect_candidates(
            raw_text, matched_nodes, segment["source_doc_id"], str(segment["segment_id"]),
        )

        seg_type = segment.get("segment_type", "unknown")
        if seg_type in _SEMANTIC_ROLE_TAGS:
            tags.append({
                "tag_type":        "semantic_role",
                "tag_value":       _SEMANTIC_ROLE_TAGS[seg_type],
                "ontology_node_id": None,
                "confidence":      1.0,
                "tagger":          "rule",
            })

        for pattern, ctx in self._context_patterns:
            if pattern.search(text[:1000]):
                tags.append({
                    "tag_type":        "context",
                    "tag_value":       ctx,
                    "ontology_node_id": None,
                    "confidence":      0.85,
                    "tagger":          "rule",
                })

        return tags, candidate_terms

    def _find_terms(self, text: str) -> list[tuple[str, str, float]]:
        """Rule A1: exact & alias match with word-boundary awareness.

        Short aliases (<=3 chars) require strict word-boundary match to avoid
        false positives like 'sp' matching inside 'specification'.
        """
        found: list[tuple[str, str, float]] = []
        text_lower = text.lower()
        ontology = self._ontology

        for surface, node_id in ontology.alias_map.items():
            if len(surface) <= 3:
                # Strict word-boundary match for short terms (IP, TCP, BGP, ...)
                if not re.search(r"\b" + re.escape(surface) + r"\b", text_lower):
                    continue
            else:
                if surface not in text_lower:
                    continue

            node = ontology.get_node_dict(node_id)
            if node and node.get("canonical_name", "").lower() == surface:
                found.append((surface, node_id, 1.0))
            else:
                found.append((surface, node_id, 0.90))

        return found

    # ── Embedding-based caches (class-level, loaded once) ────────

    _onto_embeddings = None
    _onto_node_ids = None

    def _ensure_onto_embeddings(self):
        """Lazily compute and cache ontology node embeddings."""
        if self.__class__._onto_embeddings is not None:
            return True
        from src.config.settings import settings
        if not getattr(settings, "EMBEDDING_ENABLED", False):
            return False
        try:
            from src.utils.embedding import get_embeddings
            ontology = self._ontology
            nodes = [n for n in ontology.nodes.values() if n.get("canonical_name")]
            if not nodes:
                return False
            texts = [n["canonical_name"].lower() for n in nodes]
            vecs = get_embeddings(texts)
            if vecs is None:
                return False
            import numpy as np
            self.__class__._onto_embeddings = np.array(vecs)
            self.__class__._onto_node_ids = [n["node_id"] for n in nodes]
            log.info("Cached embeddings for %d ontology nodes", len(nodes))
            return True
        except Exception as exc:
            log.debug("Embedding init failed: %s", exc)
            return False

    def _embedding_match(self, text: str) -> list[tuple[str, float]]:
        """Semantic fallback: match segment text against ontology node embeddings.

        Only called when exact alias matching yields 0 canonical tags.
        Returns list of (node_id, confidence) for matches above threshold.
        """
        if not self._ensure_onto_embeddings():
            return []
        try:
            from src.utils.embedding import get_embeddings
            import numpy as np
            # Encode segment text (truncated for efficiency)
            vecs = get_embeddings([text[:512]])
            if not vecs:
                return []
            seg_vec = np.array(vecs[0])
            similarities = np.dot(self.__class__._onto_embeddings, seg_vec)
            # Top matches above threshold
            THRESHOLD = 0.80
            MAX_MATCHES = 3
            top_indices = np.argsort(similarities)[::-1][:MAX_MATCHES]
            results = []
            for idx in top_indices:
                sim = float(similarities[idx])
                if sim >= THRESHOLD:
                    node_id = self.__class__._onto_node_ids[idx]
                    confidence = round(0.60 + (sim - THRESHOLD) * 2, 2)  # 0.80→0.60, 1.0→1.0
                    confidence = min(confidence, 0.80)  # cap at 0.80 for embedding matches
                    results.append((node_id, confidence))
                    log.debug("  Embedding match: %s (sim=%.3f conf=%.2f)",
                              node_id, sim, confidence)
            return results
        except Exception as exc:
            log.debug("Embedding match failed: %s", exc)
            return []

    def _collect_candidates(
        self, text: str, matched_nodes: dict, source_doc_id: str, segment_id: str,
    ) -> int:
        """Rule A3: discover terms not in ontology → candidate pool.

        LLM extracts AND classifies terms. No regex fallback — when LLM is
        unavailable, this segment produces zero candidates (quality over quantity).
        """
        ontology = self._ontology

        if not self._llm or not hasattr(self._llm, "extract_candidate_terms"):
            return 0

        known = [n["canonical_name"] for n in ontology.nodes.values() if n.get("canonical_name")]
        llm_results = self._llm.extract_candidate_terms(text, known)
        if not llm_results:
            return 0

        # Only keep new_concept — discard variant and noise
        # Each candidate carries its knowledge_layer from LLM classification
        _VALID_LAYERS = {"concept", "mechanism", "method", "condition", "scenario"}
        new_concepts: list[tuple[str, str]] = []  # (term, layer)
        variant_count = 0
        noise_count = 0
        for item in llm_results:
            classification = item.get("classification", "new_concept")
            term = item.get("term", "").strip()
            if not term:
                continue
            if classification == "noise":
                noise_count += 1
                continue
            if classification == "variant":
                variant_count += 1
                continue
            # new_concept: verify not already in ontology
            if ontology.lookup_alias(term.lower()):
                continue
            layer = item.get("knowledge_layer", "concept")
            if layer not in _VALID_LAYERS:
                layer = "concept"
            new_concepts.append((term, layer))

        if variant_count or noise_count:
            log.debug("  LLM classified: %d new, %d variant, %d noise",
                      len(new_concepts), variant_count, noise_count)

        # Stopword filter (last insurance against LLM misclassification)
        terms_only = [t for t, _ in new_concepts]
        filtered_terms = self._filter_stopwords(terms_only)
        filtered_terms = self._embedding_dedup(filtered_terms)
        # Rebuild (term, layer) pairs for surviving terms
        filtered_set = set(filtered_terms)
        filtered = [(t, l) for t, l in new_concepts if t in filtered_set]

        if filtered:
            log.debug("  %d candidates after filtering (from %d LLM results)",
                      len(filtered), len(llm_results))
            self._upsert_candidates(filtered, source_doc_id, segment_id)

        return len(filtered)

    # ── Candidate filters ──────────────────────────────────────────

    _stopwords: set[str] | None = None

    def _load_stopwords(self) -> set[str]:
        """Load stopword list from ontology/patterns/candidate_stopwords.yaml."""
        if self.__class__._stopwords is not None:
            return self.__class__._stopwords
        import yaml
        from pathlib import Path
        sw_path = Path(__file__).resolve().parents[3] / "ontology" / "patterns" / "candidate_stopwords.yaml"
        try:
            data = yaml.safe_load(sw_path.read_text(encoding="utf-8")) or {}
            self.__class__._stopwords = set(data.get("stopwords", []))
            log.debug("Loaded %d stopwords", len(self.__class__._stopwords))
        except FileNotFoundError:
            log.warning("Stopword file not found: %s", sw_path)
            self.__class__._stopwords = set()
        return self.__class__._stopwords

    def _filter_stopwords(self, terms: list[str]) -> list[str]:
        """Remove obvious non-concept terms via stopword list."""
        from src.utils.normalize import normalize_term
        stopwords = self._load_stopwords()
        if not stopwords:
            return terms
        result = []
        for term in terms:
            normalized = normalize_term(term)
            tokens = normalized.split()
            # Single-token candidate that is a stopword → drop
            if len(tokens) == 1 and tokens[0] in stopwords:
                log.debug("  Stopword filtered: %s", term)
                continue
            # All tokens are stopwords → drop
            if all(t in stopwords for t in tokens):
                log.debug("  All-stopword filtered: %s", term)
                continue
            result.append(term)
        return result

    def _embedding_dedup(self, terms: list[str]) -> list[str]:
        """Deduplicate candidates against ontology nodes and existing candidates using embeddings.

        Requires EMBEDDING_ENABLED=true. Falls back gracefully (returns terms unchanged) if
        embedding model is not available.
        """
        from src.config.settings import settings
        if not getattr(settings, "EMBEDDING_ENABLED", False):
            return terms

        try:
            from src.utils.embedding import get_embedding_model
            model = get_embedding_model()
            if model is None:
                return terms
        except Exception:
            return terms

        ontology = self._ontology
        store = self._store

        # Build reference texts: ontology node canonical names
        ref_texts = []
        ref_labels = []
        for node in ontology.nodes.values():
            if node.get("canonical_name"):
                ref_texts.append(node["canonical_name"].lower())
                ref_labels.append(node["node_id"])

        # Add existing candidates (top 500 by source_count)
        try:
            rows = store.fetchall(
                """SELECT normalized_form FROM governance.evolution_candidates
                   WHERE review_status NOT IN ('rejected')
                   ORDER BY source_count DESC LIMIT 500"""
            )
            existing_candidates = {r["normalized_form"] for r in rows}
        except Exception:
            existing_candidates = set()

        for nf in existing_candidates:
            ref_texts.append(nf)
            ref_labels.append(f"candidate:{nf}")

        if not ref_texts:
            return terms

        # Encode references and candidates
        try:
            import numpy as np
            ref_embeddings = model.encode(ref_texts, normalize_embeddings=True)
            term_texts = [t.lower() for t in terms]
            term_embeddings = model.encode(term_texts, normalize_embeddings=True)
        except Exception as exc:
            log.warning("Embedding dedup failed: %s", exc)
            return terms

        THRESHOLD = 0.85
        result = []
        for i, term in enumerate(terms):
            similarities = np.dot(ref_embeddings, term_embeddings[i])
            max_idx = int(np.argmax(similarities))
            max_sim = float(similarities[max_idx])
            if max_sim >= THRESHOLD:
                log.debug("  Embedding dedup: '%s' similar to '%s' (%.3f), skipping",
                          term, ref_labels[max_idx], max_sim)
                continue
            result.append(term)

        if len(result) < len(terms):
            log.info("  Embedding dedup: %d → %d candidates", len(terms), len(result))
        return result

    def _upsert_candidates(
        self, terms: list[tuple[str, str]], source_doc_id: str, segment_id: str,
    ) -> None:
        """Write candidate terms to governance.evolution_candidates.

        Args:
            terms: List of (term_text, knowledge_layer) tuples.
            source_doc_id: Document ID for traceability.
            segment_id: Segment ID for traceability.

        Records segment_id + source_doc_id in examples JSONB for traceability.
        Extracts parenthetical abbreviations as extra surface_forms.
        """
        import json
        from src.utils.normalize import normalize_term, extract_abbreviation
        _LAYER_TO_TYPE = {
            "concept": "concept", "mechanism": "mechanism", "method": "method",
            "condition": "condition", "scenario": "scenario",
        }
        store = self._store
        seen = set()
        for term, layer in terms:
            if term in seen:
                continue
            seen.add(term)
            candidate_type = _LAYER_TO_TYPE.get(layer, "concept")
            normalized = normalize_term(term)
            # Extract abbreviation if present: "xxx (YYY)" → also add "YYY" as surface_form
            abbrev = extract_abbreviation(term)
            # Clean the term (strip parenthetical for primary surface_form)
            clean_term = re.sub(r"\s*\([^)]*\)\s*", "", term).strip() or term
            # Build initial surface_forms array
            initial_forms = [clean_term]
            if abbrev and abbrev != clean_term:
                initial_forms.append(abbrev)

            example = json.dumps([{"segment_id": segment_id, "source_doc_id": source_doc_id}])
            store.execute(
                """
                INSERT INTO governance.evolution_candidates
                    (surface_forms, normalized_form, candidate_type, source_count, last_seen_at,
                     first_seen_at, seen_source_doc_ids, review_status, examples)
                VALUES (%s, %s, %s, 1, NOW(), NOW(), ARRAY[%s::uuid], 'discovered', %s::jsonb)
                ON CONFLICT (normalized_form) DO UPDATE SET
                    source_count = governance.evolution_candidates.source_count + 1,
                    last_seen_at = NOW(),
                    surface_forms = CASE
                        WHEN NOT (%s = ANY(governance.evolution_candidates.surface_forms))
                        THEN array_append(governance.evolution_candidates.surface_forms, %s)
                        ELSE governance.evolution_candidates.surface_forms
                    END,
                    seen_source_doc_ids = CASE
                        WHEN NOT (%s::uuid = ANY(governance.evolution_candidates.seen_source_doc_ids))
                        THEN array_append(governance.evolution_candidates.seen_source_doc_ids, %s::uuid)
                        ELSE governance.evolution_candidates.seen_source_doc_ids
                    END,
                    examples = governance.evolution_candidates.examples || %s::jsonb
                """,
                (initial_forms, normalized, candidate_type, source_doc_id, example,
                 clean_term, clean_term, source_doc_id, source_doc_id, example),
            )

    def _propagate_via_rst(self, source_doc_id: str, pending_seg_ids: list[str]) -> int:
        """P1b: Propagate canonical tags from aligned neighbors to pending segments via RST.

        For each segment with 0 canonical tags, look up its RST neighbors (both directions).
        If a neighbor has canonical tags, propagate them with a weight that depends on the
        RST relation type — strong elaboration/sequence neighbors are more trustworthy than
        weak contrast/background neighbors.

        Only inserts a propagated tag when adjusted confidence > 0.50.
        Returns the total number of propagated tag rows inserted.
        """
        # Propagation weights by RST relation type
        # Anchor: the neighbor is the nucleus (more reliable source of canonical concepts)
        _WEIGHTS: dict[str, float] = {
            "Elaboration":    0.85,
            "Sequence":       0.85,
            "Exemplification": 0.80,
            "Causation":      0.70,
            "Constraint":     0.70,
            "Prerequisite":   0.65,
            "Evidence":       0.60,
            "Background":     0.60,
            "Condition":      0.50,
            "Contrast":       0.30,
        }
        MIN_CONF = 0.50
        store = self._store
        total_inserted = 0

        placeholders = ",".join(["%s"] * len(pending_seg_ids))
        # Fetch all RST edges touching any pending segment (either direction)
        rst_rows = store.fetchall(
            f"""SELECT src_edu_id, dst_edu_id, relation_type, nuclearity
                FROM t_rst_relation
                WHERE src_edu_id::text IN ({placeholders})
                   OR dst_edu_id::text IN ({placeholders})""",
            (*pending_seg_ids, *pending_seg_ids),
        )

        pending_set = set(pending_seg_ids)

        for row in rst_rows:
            rel_type   = row.get("relation_type", "Elaboration")
            nuclearity = row.get("nuclearity") or "NN"
            weight     = _WEIGHTS.get(rel_type, 0.40)

            src_id = str(row["src_edu_id"])
            dst_id = str(row["dst_edu_id"])

            # Determine which is the pending segment and which is the neighbor
            if src_id in pending_set and dst_id not in pending_set:
                pending_id   = src_id
                neighbor_id  = dst_id
                # Boost if neighbor is the nucleus
                if nuclearity == "SN":  # dst is nucleus → neighbor is nucleus
                    weight = min(weight + 0.05, 1.0)
            elif dst_id in pending_set and src_id not in pending_set:
                pending_id   = dst_id
                neighbor_id  = src_id
                if nuclearity == "NS":  # src is nucleus → neighbor is nucleus
                    weight = min(weight + 0.05, 1.0)
            else:
                continue  # both pending or both aligned — skip

            if weight < MIN_CONF:
                continue

            # Fetch neighbor's canonical tags
            neighbor_tags = store.fetchall(
                """SELECT tag_value, ontology_node_id, confidence
                   FROM segment_tags
                   WHERE segment_id = %s AND tag_type = 'canonical'""",
                (neighbor_id,),
            )
            if not neighbor_tags:
                continue

            # Propagate each canonical tag with attenuated confidence
            inserted_here = 0
            for nt in neighbor_tags:
                adj_conf = round(float(nt["confidence"]) * weight, 3)
                if adj_conf < MIN_CONF:
                    continue
                node = self._ontology.get_node_dict(nt["ontology_node_id"])
                tag_value = node["canonical_name"] if node else nt["tag_value"]
                try:
                    store.execute(
                        """
                        INSERT INTO segment_tags
                          (segment_id, tag_type, tag_value, ontology_node_id,
                           confidence, tagger, ontology_version)
                        VALUES (%s, 'canonical', %s, %s, %s, 'rst_propagated', 'v0.1.0')
                        ON CONFLICT DO NOTHING
                        """,
                        (pending_id, tag_value, nt["ontology_node_id"], adj_conf),
                    )
                    inserted_here += 1
                except Exception as exc:
                    log.debug("  rst_propagate insert failed: %s", exc)

            if inserted_here:
                # Restore segment to active so Stage 4 can use it
                store.execute(
                    "UPDATE segments SET lifecycle_state='active' WHERE segment_id=%s",
                    (pending_id,),
                )
                log.debug("  rst_propagate seg=%s ← neighbor=%s rel=%s weight=%.2f tags=%d",
                          pending_id[:8], neighbor_id[:8], rel_type, weight, inserted_here)
                total_inserted += inserted_here

        if total_inserted:
            log.info("RST propagation doc=%s: %d tags added to %d pending segments",
                     source_doc_id, total_inserted, len(pending_seg_ids))
        return total_inserted

    def _save_tags(self, segment_id: str, tags: list[dict]) -> int:
        if not tags:
            return 0
        store = self._store
        with store.transaction() as cur:
            for tag in tags:
                cur.execute(
                    """
                    INSERT INTO segment_tags
                      (segment_id, tag_type, tag_value, ontology_node_id, confidence, tagger, ontology_version)
                    VALUES (%s,%s,%s,%s,%s,%s,'v0.1.0')
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        segment_id, tag["tag_type"], tag["tag_value"],
                        tag.get("ontology_node_id"), tag["confidence"], tag["tagger"],
                    ),
                )
        return len(tags)