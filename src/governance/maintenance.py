"""Periodic ontology maintenance — embedding dedup, LLM classification, knowledge merge.

Runs as an independent thread in the worker (every 24h by default).
Can also be invoked manually via scripts/clean_candidates.py.

Three-pass pipeline:
1. Embedding dedup: merge duplicate candidates, reject ontology variants
2. LLM classification: classify remaining as new_concept / variant / noise
3. Cleanup: delete noise, merge variant knowledge into existing nodes
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

from semcore.providers.base import RelationalStore

log = logging.getLogger(__name__)


class OntologyMaintenance:
    """Periodic ontology candidate maintenance."""

    def __init__(self, store: RelationalStore, graph, ontology):
        self._store = store
        self._graph = graph
        self._ontology = ontology

    def run(self, skip_embedding: bool = False, skip_llm: bool = False) -> dict:
        """Execute full maintenance cycle. Returns stats dict."""
        log.info("=== Ontology maintenance started ===")

        candidates = self._load_candidates()
        log.info("Loaded %d candidates (excluding rejected/noise)", len(candidates))
        accepted = sum(1 for c in candidates if c["review_status"] == "accepted")
        log.info("  %d accepted (untouched), %d to process", accepted, len(candidates) - accepted)

        stats = {"initial": len(candidates), "accepted_untouched": accepted}

        # Pass 1: Embedding dedup
        if not skip_embedding:
            emb_stats = self._embedding_pass(candidates)
            stats["embedding"] = emb_stats
            # Reload after changes
            candidates = self._load_candidates()
        else:
            stats["embedding"] = "skipped"

        # Pass 2: LLM classification
        if not skip_llm:
            llm_stats = self._llm_pass(candidates)
            stats["llm"] = llm_stats
        else:
            stats["llm"] = "skipped"

        # Pass 3: Cleanup (delete noise, merge variants)
        cleanup_stats = self._cleanup_pass()
        stats["cleanup"] = cleanup_stats

        # Pass 4: Refresh ontology embedding cache
        emb_refresh = self._refresh_embedding_cache()
        stats["embedding_cache"] = emb_refresh

        # Final counts
        final = self._status_counts()
        stats["final"] = final
        log.info("=== Ontology maintenance complete: %s ===", final)
        return stats

    def _load_candidates(self) -> list[dict]:
        return [dict(r) for r in self._store.fetchall(
            """SELECT candidate_id, normalized_form, surface_forms, source_count,
                      review_status, candidate_type, examples
               FROM governance.evolution_candidates
               WHERE review_status NOT IN ('rejected', 'noise_deleted', 'variant_merged')
               ORDER BY source_count DESC"""
        )]

    def _status_counts(self) -> dict:
        rows = self._store.fetchall(
            "SELECT review_status, count(*) AS cnt FROM governance.evolution_candidates GROUP BY review_status"
        )
        return {r["review_status"]: r["cnt"] for r in rows}

    # ── Pass 1: Embedding dedup ──────────────────────────────────────────────

    def _embedding_pass(self, candidates: list[dict]) -> dict:
        """Deduplicate candidates using embedding similarity."""
        log.info("Pass 1: Embedding dedup on %d candidates...", len(candidates))

        if not candidates:
            return {"status": "skipped", "reason": "no_candidates"}

        try:
            import numpy as np
            from src.utils.embedding import get_embeddings
        except ImportError:
            log.warning("numpy not available, skipping embedding pass")
            return {"status": "skipped"}

        # Encode candidates using rich text (name + description + suggested aliases)
        import json as _json
        texts = []
        for c in candidates:
            name = c.get("normalized_form", "")
            desc = c.get("description") or ""
            sa = c.get("suggested_aliases")
            if isinstance(sa, str):
                try:
                    sa = _json.loads(sa)
                except Exception:
                    sa = []
            sa = sa or []
            surface_forms = c.get("surface_forms") or []
            parts = [name]
            if desc:
                parts.append(desc)
            all_aliases = list(set(surface_forms) | set(sa))
            if all_aliases:
                parts.append("Aliases: " + ", ".join(all_aliases))
            texts.append(". ".join(parts))
        log.info("  Encoding %d candidates (rich text)...", len(texts))
        raw_emb = get_embeddings(texts)
        if raw_emb is None:
            log.warning("  Embedding backend unavailable")
            return {"status": "unavailable"}
        embeddings = np.array(raw_emb)
        norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1
        embeddings = embeddings / norms
        log.info("  Encoded: shape=%s", embeddings.shape)

        # Encode ontology nodes
        onto_names, onto_ids, onto_layers = self._get_ontology_terms()
        log.info("  Encoding %d ontology terms...", len(onto_names))
        raw_onto = get_embeddings([t.lower() for t in onto_names])
        if raw_onto is None:
            log.warning("  Failed to encode ontology terms")
            return {"status": "onto_encode_failed"}
        onto_emb = np.array(raw_onto)
        onto_norms = np.linalg.norm(onto_emb, axis=1, keepdims=True)
        onto_norms[onto_norms == 0] = 1
        onto_emb = onto_emb / onto_norms

        # Layer mapping: candidate_type → allowed target node layer
        # 'concept' candidates may only merge into ConceptNode (IP.*) targets.
        # 'relation' candidates aren't matched against nodes here at all.
        # Without this filter, "subnet" embeds close to "RouteRedistribution"
        # because BGE-M3 places routing-vocabulary in the same neighbourhood,
        # and the cleanup pass then adds bad aliases like subnet→MECH.RouteRedistribution.
        CAND_TYPE_TO_LAYER = {"concept": "concept"}
        # Raised from 0.85 to 0.90 — cosine similarity over BGE-M3 in the same
        # subdomain is naturally high; 0.85 produced too many false matches.
        THRESHOLD = 0.90

        # Detect ontology variants (only same-layer matches are eligible)
        sim_onto = np.dot(embeddings, onto_emb.T)
        onto_variant_count = 0
        for i, cand in enumerate(candidates):
            if cand["review_status"] == "accepted":
                continue
            allowed_layer = CAND_TYPE_TO_LAYER.get(cand.get("candidate_type", ""))
            if allowed_layer is None:
                continue  # don't match against ontology nodes for non-concept candidates
            # Mask out targets that are NOT in the allowed layer
            sims_i = sim_onto[i].copy()
            for j, lyr in enumerate(onto_layers):
                if lyr != allowed_layer:
                    sims_i[j] = -1.0
            max_idx = int(np.argmax(sims_i))
            max_sim = float(sims_i[max_idx])
            if max_sim >= THRESHOLD:
                matched_node_id = onto_ids[max_idx]
                self._store.execute(
                    """UPDATE governance.evolution_candidates
                       SET review_status = 'auto_rejected',
                           review_note = %s
                       WHERE candidate_id = %s
                         AND review_status NOT IN ('accepted', 'rejected')""",
                    (f"embedding_variant:{matched_node_id}:{max_sim:.3f}",
                     cand["candidate_id"]),
                )
                onto_variant_count += 1

        log.info("  %d ontology variants detected", onto_variant_count)

        # Cluster duplicate candidates
        remaining = [i for i in range(len(candidates))
                     if candidates[i]["review_status"] not in ("accepted",)
                     and i not in {i for i, c in enumerate(candidates)
                                   if c["review_status"] == "accepted"}]

        # Simple union-find clustering
        parent = list(range(len(remaining)))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        sub_emb = embeddings[remaining]
        sim = np.dot(sub_emb, sub_emb.T)
        for i in range(len(remaining)):
            for j in range(i + 1, len(remaining)):
                if sim[i, j] >= THRESHOLD:
                    union(i, j)

        clusters: dict[int, list[int]] = defaultdict(list)
        for i in range(len(remaining)):
            clusters[find(i)].append(remaining[i])
        merge_clusters = {k: v for k, v in clusters.items() if len(v) > 1}

        merged_count = 0
        for indices in merge_clusters.values():
            cluster_cands = [candidates[i] for i in indices]
            cluster_cands.sort(key=lambda c: -(c.get("source_count") or 0))
            primary = cluster_cands[0]
            others = cluster_cands[1:]

            all_forms = list(primary.get("surface_forms") or [])
            total_count = int(primary.get("source_count") or 0)
            for other in others:
                for sf in (other.get("surface_forms") or []):
                    if sf not in all_forms:
                        all_forms.append(sf)
                total_count += int(other.get("source_count") or 0)

            for other in others:
                self._store.execute(
                    "DELETE FROM governance.evolution_candidates WHERE candidate_id = %s",
                    (other["candidate_id"],),
                )
            self._store.execute(
                """UPDATE governance.evolution_candidates
                   SET surface_forms = %s, source_count = %s
                   WHERE candidate_id = %s""",
                (all_forms, total_count, primary["candidate_id"]),
            )
            merged_count += len(others)

        log.info("  %d duplicates merged across %d clusters", merged_count, len(merge_clusters))
        return {
            "onto_variants": onto_variant_count,
            "duplicates_merged": merged_count,
            "clusters": len(merge_clusters),
        }

    # ── Pass 2: LLM classification ──────────────────────────────────────────

    def _llm_pass(self, candidates: list[dict]) -> dict:
        """Batch-classify candidates as new_concept / variant / noise."""
        to_classify = [c for c in candidates
                       if c["review_status"] == "discovered"]
        log.info("Pass 2: LLM classification on %d candidates...", len(to_classify))

        if not to_classify:
            return {"classified": 0}

        try:
            from src.utils.llm_extract import LLMExtractor
            llm = LLMExtractor()
            if not llm.is_enabled():
                log.warning("  LLM not enabled")
                return {"status": "llm_disabled"}
        except Exception as exc:
            log.warning("  LLM init failed: %s", exc)
            return {"status": "llm_error"}

        onto_names, _ = self._get_ontology_terms()
        known_sample = ", ".join(onto_names[:100])

        BATCH_SIZE = 20
        total_new, total_variant, total_noise, total_error = 0, 0, 0, 0

        for batch_start in range(0, len(to_classify), BATCH_SIZE):
            batch = to_classify[batch_start:batch_start + BATCH_SIZE]
            batch_terms = [
                f"{i+1}. {(c['surface_forms'] or [c['normalized_form']])[0]} (seen {c['source_count']}x)"
                for i, c in enumerate(batch)
            ]

            system = (
                "You are a network engineering ontology curator.\n"
                "Classify each candidate term below as:\n"
                "- new_concept: a standalone networking/telecom concept worthy of its own ontology entry\n"
                "- variant: a qualified/contextual form of an EXISTING known concept "
                "(e.g. 'OSPF router ID' is a variant of 'router ID'). "
                "Respond with parent_concept name.\n"
                "- noise: generic word, document structure, non-domain term\n\n"
                "Return ONLY a JSON array:\n"
                '[{"index": 1, "classification": "new_concept|variant|noise", '
                '"parent_concept": "<if variant>", "reason": "<brief>"}]\n\n'
                "Be strict: when in doubt, classify as variant or noise."
            )
            prompt = (
                f"Known ontology concepts: {known_sample}\n\n"
                f"Candidate terms:\n" + "\n".join(batch_terms) +
                "\n\nClassify as JSON array:"
            )

            try:
                raw = llm._call_llm(system, prompt, 1024)
                if raw is None:
                    total_error += len(batch)
                    time.sleep(2)
                    continue

                classifications = self._parse_batch(raw, len(batch))
                for i, cand in enumerate(batch):
                    cls = classifications.get(i + 1, {})
                    classification = cls.get("classification", "new_concept")
                    parent = cls.get("parent_concept", "")

                    if classification == "noise":
                        total_noise += 1
                        self._store.execute(
                            """UPDATE governance.evolution_candidates
                               SET review_status = 'auto_rejected',
                                   review_note = 'llm:noise'
                               WHERE candidate_id = %s""",
                            (cand["candidate_id"],),
                        )
                    elif classification == "variant":
                        total_variant += 1
                        self._store.execute(
                            """UPDATE governance.evolution_candidates
                               SET review_status = 'auto_rejected',
                                   review_note = %s
                               WHERE candidate_id = %s""",
                            (f"llm:variant:{parent}", cand["candidate_id"]),
                        )
                    else:
                        total_new += 1

            except Exception as exc:
                log.warning("  LLM batch error: %s", exc)
                total_error += len(batch)

            time.sleep(1)

            if (batch_start // BATCH_SIZE) % 10 == 0 and batch_start > 0:
                log.info("  Progress: %d/%d (new=%d, variant=%d, noise=%d, error=%d)",
                         batch_start + len(batch), len(to_classify),
                         total_new, total_variant, total_noise, total_error)

        log.info("  LLM done: new=%d, variant=%d, noise=%d, error=%d",
                 total_new, total_variant, total_noise, total_error)
        return {
            "new_concept": total_new,
            "variant": total_variant,
            "noise": total_noise,
            "error": total_error,
        }

    # ── Pass 3: Cleanup ─────────────────────────────────────────────────────

    def _cleanup_pass(self) -> dict:
        """Delete noise, merge variant knowledge into existing ontology nodes."""
        log.info("Pass 3: Cleanup...")
        store = self._store

        # 3a: Delete noise (handle both formats: 'llm:noise' and 'llm: noise')
        noise_rows = store.fetchall(
            """SELECT candidate_id FROM governance.evolution_candidates
               WHERE review_status = 'auto_rejected'
                 AND (review_note LIKE 'llm:noise' OR review_note LIKE 'llm: noise')"""
        )
        for r in noise_rows:
            store.execute(
                "DELETE FROM governance.evolution_candidates WHERE candidate_id = %s",
                (r["candidate_id"],),
            )
        log.info("  Deleted %d noise candidates", len(noise_rows))

        # 3b: Merge variant knowledge into existing ontology nodes
        variant_rows = store.fetchall(
            """SELECT candidate_id, normalized_form, surface_forms, examples, review_note
               FROM governance.evolution_candidates
               WHERE review_status = 'auto_rejected'
                 AND (review_note LIKE 'llm:variant:%%'
                   OR review_note LIKE 'llm: variant%%'
                   OR review_note LIKE 'embedding_variant:%%'
                   OR review_note LIKE 'embedding: ontology variant%%')"""
        )
        merged_count = 0
        for vr in variant_rows:
            note = vr.get("review_note") or ""
            # Extract matched node_id from review_note
            matched_node_id = self._extract_matched_node(note)
            if not matched_node_id:
                # Can't determine target node, just delete
                store.execute(
                    "DELETE FROM governance.evolution_candidates WHERE candidate_id = %s",
                    (vr["candidate_id"],),
                )
                continue

            # Merge knowledge: add segment tags for the matched node
            examples = vr.get("examples") or []
            if isinstance(examples, str):
                try:
                    examples = json.loads(examples)
                except Exception:
                    examples = []

            tags_added = 0
            for ex in examples:
                seg_id = ex.get("segment_id")
                if not seg_id:
                    continue
                # Add canonical tag pointing to the existing node
                store.execute(
                    """INSERT INTO segment_tags
                         (segment_id, tag_type, tag_value, ontology_node_id,
                          confidence, tagger, ontology_version)
                       VALUES (%s, 'canonical', %s, %s, 0.75, 'maintenance', 'evolved')
                       ON CONFLICT DO NOTHING""",
                    (seg_id, matched_node_id, matched_node_id),
                )
                tags_added += 1

            # Add surface forms as aliases for the existing node
            surface_forms = vr.get("surface_forms") or []
            aliases_added = self._add_aliases(matched_node_id, surface_forms)

            # Delete the candidate
            store.execute(
                "UPDATE governance.evolution_candidates SET review_status = 'variant_merged', "
                "review_note = %s WHERE candidate_id = %s",
                (f"merged_into:{matched_node_id}", vr["candidate_id"]),
            )
            merged_count += 1

            if tags_added > 0 or aliases_added > 0:
                log.debug("  Merged '%s' → %s (tags=%d, aliases=%d)",
                          vr["normalized_form"], matched_node_id, tags_added, aliases_added)

        log.info("  Merged %d variants into existing nodes, deleted %d noise",
                 merged_count, len(noise_rows))

        # 3c: Stale cleanup — candidates seen only once, >30 days old
        stale = store.fetchone(
            """SELECT count(*) AS cnt FROM governance.evolution_candidates
               WHERE review_status = 'discovered'
                 AND source_count = 1
                 AND last_seen_at < NOW() - INTERVAL '30 days'"""
        )
        stale_count = stale["cnt"] if stale else 0
        if stale_count > 0:
            store.execute(
                """DELETE FROM governance.evolution_candidates
                   WHERE review_status = 'discovered'
                     AND source_count = 1
                     AND last_seen_at < NOW() - INTERVAL '30 days'"""
            )
            log.info("  Deleted %d stale candidates (1 source, >30 days)", stale_count)

        return {
            "noise_deleted": len(noise_rows),
            "variants_merged": merged_count,
            "stale_deleted": stale_count,
        }

    def _extract_matched_node(self, review_note: str) -> str | None:
        """Extract matched ontology node_id from review_note.

        Formats:
          'embedding_variant:IP.BGP:0.912'
          'embedding: ontology variant'       (old format, no node_id)
          'llm:variant:BGP'                   (new format, has parent name)
          'llm: variant'                      (old format, no parent name)
        """
        if not review_note:
            return None

        # New format: 'embedding_variant:IP.BGP:0.912'
        if review_note.startswith("embedding_variant:"):
            parts = review_note.split(":")
            if len(parts) >= 2:
                return parts[1]

        # New format: 'llm:variant:BGP'
        if review_note.startswith("llm:variant:"):
            parent_name = review_note[len("llm:variant:"):].strip()
            return self._resolve_name_to_node(parent_name)

        # Old format: 'llm: variant' (no parent name — try fuzzy match by candidate name)
        # Can't determine target, return None (will be deleted, not merged)

        # Old format: 'embedding: ontology variant' (no node_id)
        # Can't determine target, return None

        return None

    def _resolve_name_to_node(self, name: str) -> str | None:
        """Try to resolve a concept name to a node_id."""
        if not name:
            return None
        name_lower = name.lower().strip()
        # Via alias_map
        if hasattr(self._ontology, "alias_map"):
            node_id = self._ontology.alias_map.get(name_lower)
            if node_id:
                return node_id
        # Direct name match
        if hasattr(self._ontology, "nodes"):
            for nid, node in self._ontology.nodes.items():
                if (node.get("canonical_name") or "").lower() == name_lower:
                    return nid
        return None

    def _add_aliases(self, node_id: str, surface_forms: list[str]) -> int:
        """Add surface forms as aliases for an existing ontology node.

        Validates each alias against corpus frequency: if it matches more than
        5% of active segments, it's too generic and gets rejected.
        """
        import uuid
        added = 0
        for sf in surface_forms:
            sf_lower = sf.strip().lower()
            if len(sf_lower) < 2:
                continue
            if hasattr(self._ontology, "alias_map") and sf_lower in self._ontology.alias_map:
                continue
            if self._is_too_generic(sf_lower):
                log.debug("Alias '%s' rejected: too generic (high corpus frequency)", sf_lower)
                continue
            alias_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{node_id}:{sf_lower}"))
            try:
                self._store.execute(
                    """INSERT INTO lexicon_aliases
                         (alias_id, surface_form, canonical_node_id, alias_type, language)
                       VALUES (%s, %s, %s, 'maintenance', 'en')
                       ON CONFLICT (surface_form, canonical_node_id) DO NOTHING""",
                    (alias_id, sf_lower, node_id),
                )
                if hasattr(self._ontology, "alias_map"):
                    self._ontology.alias_map[sf_lower] = node_id
                added += 1
            except Exception:
                pass
        return added

    def _is_too_generic(self, surface_form: str) -> bool:
        """Reject alias if it matches too many segments (>5% of active corpus)."""
        import re
        total = self._store.fetchone(
            "SELECT count(*) AS c FROM segments WHERE lifecycle_state='active'"
        )
        total_count = total["c"] if total else 0
        if total_count == 0:
            return False
        if len(surface_form) <= 3:
            pattern = r'\m' + re.escape(surface_form) + r'\M'
            hit = self._store.fetchone(
                "SELECT count(*) AS c FROM segments WHERE lifecycle_state='active' "
                "AND normalized_text ~* %s",
                (pattern,),
            )
        else:
            hit = self._store.fetchone(
                "SELECT count(*) AS c FROM segments WHERE lifecycle_state='active' "
                "AND normalized_text ILIKE %s",
                (f"%{surface_form}%",),
            )
        hit_count = hit["c"] if hit else 0
        hit_rate = hit_count / total_count
        if hit_rate > 0.05:
            log.info("Alias '%s' hit %d/%d segments (%.1f%%) - rejected as too generic",
                     surface_form, hit_count, total_count, hit_rate * 100)
            return True
        return False

    def _get_ontology_terms(self) -> tuple[list[str], list[str], list[str]]:
        """Return (names, node_ids, layers) from ontology registry."""
        from src.ontology.registry import OntologyRegistry
        reg = OntologyRegistry.from_default()
        names, ids, layers = [], [], []
        for nid, n in reg.nodes.items():
            name = n.get("canonical_name")
            if name:
                names.append(name)
                ids.append(nid)
                layers.append(n.get("knowledge_layer", "concept"))
        return names, ids, layers

    @staticmethod
    def _parse_batch(raw: str, expected: int) -> dict[int, dict]:
        """Parse LLM batch response → {index: {classification, parent_concept}}."""
        import re
        raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.MULTILINE)
        raw = re.sub(r"\s*```$", "", raw, flags=re.MULTILINE).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            m = re.search(r"\[.*\]", raw, re.DOTALL)
            if not m:
                return {}
            try:
                data = json.loads(m.group())
            except json.JSONDecodeError:
                return {}
        if not isinstance(data, list):
            return {}
        valid = {"new_concept", "variant", "noise"}
        result = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            idx = item.get("index")
            cls = item.get("classification", "")
            if isinstance(idx, int) and cls in valid:
                result[idx] = {
                    "classification": cls,
                    "parent_concept": item.get("parent_concept", ""),
                }
        return result

    def _refresh_embedding_cache(self) -> dict:
        """Rebuild ontology embedding cache if node list has changed."""
        from pathlib import Path
        import numpy as np
        from src.config.settings import settings

        if not getattr(settings, "EMBEDDING_ENABLED", False):
            return {"status": "skipped", "reason": "embedding_disabled"}

        cache_path = Path(__file__).resolve().parents[2] / "tmp" / "onto_embeddings.npz"
        ontology = self._ontology
        nodes = [n for n in ontology.nodes.values() if n.get("canonical_name")]
        current_ids = [n.get("node_id") or n["id"] for n in nodes]

        if cache_path.exists():
            try:
                cached_ids = list(np.load(cache_path, allow_pickle=True)["node_ids"])
                if cached_ids == current_ids:
                    log.info("Embedding cache up to date (%d nodes)", len(current_ids))
                    return {"status": "up_to_date", "nodes": len(current_ids)}
            except Exception:
                pass

        log.info("Ontology changed, rebuilding embedding cache for %d nodes x2 langs...", len(current_ids))
        try:
            from src.utils.embedding import get_embeddings
            en_texts = []
            zh_texts = []
            for n in nodes:
                name = n["canonical_name"].lower()
                desc = n.get("description", "").strip()
                en_part = ""
                zh_part = ""
                for p in desc.split(". ", 1) if ". " in desc else [desc]:
                    p = p.strip()
                    if any('\u4e00' <= c <= '\u9fff' for c in p[:20]):
                        zh_part = p
                    else:
                        en_part = p
                en_texts.append((name + ". " + en_part).strip() if en_part else name)
                zh_texts.append((n.get("display_name_zh", "") + " " + zh_part).strip() if zh_part else name)
            vecs = get_embeddings(en_texts + zh_texts)
            if vecs is None:
                return {"status": "failed", "reason": "embedding_service_unavailable"}
            arr = np.array(vecs)
            emb_en = arr[:len(nodes)]
            emb_zh = arr[len(nodes):]
            node_layers = [n.get("knowledge_layer", "concept") for n in nodes]
            cache_path.parent.mkdir(exist_ok=True)
            np.savez(cache_path, embeddings_en=emb_en, embeddings_zh=emb_zh,
                     node_ids=np.array(current_ids), node_layers=np.array(node_layers))

            from src.pipeline.stages.stage3_align import AlignStage
            AlignStage._onto_embeddings = emb_en
            AlignStage._onto_embeddings_zh = emb_zh
            AlignStage._onto_node_ids = current_ids
            AlignStage._onto_node_layers = node_layers

            log.info("Embedding cache rebuilt and hot-reloaded (%d nodes)", len(current_ids))
            return {"status": "rebuilt", "nodes": len(current_ids)}
        except Exception as exc:
            log.warning("Embedding cache rebuild failed: %s", exc)
            return {"status": "failed", "reason": str(exc)}