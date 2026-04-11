"""Stage 4: Relation extraction + fact construction - rules R1-R4 + LLM."""

from __future__ import annotations

import logging
import re
import uuid

from semcore.core.context import PipelineContext
from semcore.pipeline.base import Stage
from semcore.providers.base import RelationalStore

from src.utils.confidence import score_fact

log = logging.getLogger(__name__)

# Predicate signal patterns loaded from ontology/patterns/predicate_signals.yaml
# (no hardcoded patterns — loaded at runtime via OntologyRegistry)
# NOTE: regex relation extraction removed — LLM-only with co-occurrence fallback


class ExtractStage(Stage):
    name = "extract"

    def __init__(self) -> None:
        self._ontology = None
        self._llm = None
        self._store: RelationalStore | None = None

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._ontology = app.ontology
        self._llm = app.llm
        self._store = app.store
        self._crawler_store = getattr(app, "crawler_store", None) or app.store
        self._predicate_signals = getattr(app.ontology, "predicate_signal_patterns", [])
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        facts = self._run(source_doc_id)
        self.set_output(ctx, {"facts": facts})
        return ctx

    def _run(self, source_doc_id: str) -> list[dict]:
        """Extract facts from all segments of a document."""
        store = self._store
        doc = store.fetchall(
            "SELECT source_rank FROM documents WHERE source_doc_id=%s", (source_doc_id,)
        )
        source_rank = doc[0]["source_rank"] if doc else "C"

        segments = store.fetchall(
            "SELECT * FROM segments WHERE source_doc_id=%s AND lifecycle_state='active'",
            (source_doc_id,),
        )
        # Enrich each segment with its canonical ontology tags
        for seg in segments:
            tags = store.fetchall(
                "SELECT ontology_node_id FROM segment_tags "
                "WHERE segment_id=%s AND tag_type='canonical'",
                (seg["segment_id"],),
            )
            seg["canonical_nodes"] = [
                t["ontology_node_id"] for t in tags if t.get("ontology_node_id")
            ]

        log.info("Extract start doc=%s: segments=%d rank=%s llm=%s",
                 source_doc_id, len(segments), source_rank,
                 "enabled" if self._llm.is_enabled() else "disabled")
        all_facts: list[dict] = []
        llm_count = 0
        rule_count = 0

        cooccurrence_count = 0
        merged_count = 0
        for i, seg in enumerate(segments):
            # Priority 1: LLM extraction (highest quality)
            llm_facts = self.extract_facts_llm(seg, source_rank)
            if llm_facts:
                all_facts.extend(llm_facts)
                llm_count += len(llm_facts)
                log.debug("  seg=%s llm=%d", str(seg["segment_id"])[:12], len(llm_facts))
                continue

            # Priority 2: LLM with merged context — combine with previous segment
            # if RST relation is continuative (Elaboration/Sequence/Restatement/Explanation)
            if i > 0:
                merged_facts = self._extract_merged_context(
                    segments[i - 1], seg, source_rank, source_doc_id,
                )
                if merged_facts:
                    all_facts.extend(merged_facts)
                    merged_count += len(merged_facts)
                    log.debug("  seg=%s merged=%d", str(seg["segment_id"])[:12], len(merged_facts))
                    continue

            # Priority 3: Co-occurrence (last resort, low quality)
            cooc_facts = self._extract_cooccurrence(seg, source_rank)
            if cooc_facts:
                all_facts.extend(cooc_facts)
                cooccurrence_count += len(cooc_facts)
                log.debug("  seg=%s cooccurrence=%d", str(seg["segment_id"])[:12], len(cooc_facts))

        self._save_facts(all_facts, source_doc_id)
        self._crawler_store.execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) "
            "VALUES ('dedup',%s,'pending','0.2.0')",
            (source_doc_id,),
        )
        fact_ids = [f["fact_id"] for f in all_facts]
        id_preview = self._preview_ids(fact_ids)
        log.info(
            "Extracted facts doc=%s total=%d llm=%d merged=%d cooccurrence=%d fact_ids=%s",
            source_doc_id,
            len(all_facts),
            llm_count,
            merged_count,
            cooccurrence_count,
            id_preview,
        )
        return all_facts

    def _extract_merged_context(
        self, prev_seg: dict, curr_seg: dict, source_rank: str, source_doc_id: str,
    ) -> list[dict]:
        """Priority 2: Merge with previous segment and retry LLM.

        Only triggers when the RST relation between prev and curr is continuative
        (Elaboration, Sequence, Restatement, Explanation), meaning they form
        a semantic unit that was split by segmentation.
        """
        store = self._store
        continuative_types = {"Elaboration", "Sequence", "Restatement", "Explanation",
                              "Background", "Evidence", "Means"}

        # Check RST relation between prev and curr
        rst_row = store.fetchone(
            """SELECT relation_type FROM t_rst_relation
               WHERE src_edu_id = %s AND dst_edu_id = %s LIMIT 1""",
            (str(prev_seg["segment_id"]), str(curr_seg["segment_id"])),
        )
        if not rst_row or rst_row.get("relation_type") not in continuative_types:
            return []

        # Merge texts and retry LLM
        merged_text = (
            (prev_seg.get("raw_text") or "") + "\n" +
            (curr_seg.get("raw_text") or "")
        )
        # Build merged segment dict for LLM
        merged_seg = {
            **curr_seg,
            "raw_text": merged_text,
            "normalized_text": merged_text.lower(),
            "canonical_nodes": list(set(
                (prev_seg.get("canonical_nodes") or []) +
                (curr_seg.get("canonical_nodes") or [])
            )),
        }
        facts = self.extract_facts_llm(merged_seg, source_rank)
        if facts:
            log.debug("  merged context (%s): %s + %s → %d facts",
                      rst_row["relation_type"],
                      str(prev_seg["segment_id"])[:8],
                      str(curr_seg["segment_id"])[:8],
                      len(facts))
        return facts

    def _extract_cooccurrence(self, segment: dict, source_rank: str) -> list[dict]:
        """Priority 3 (last resort): Co-occurrence when regex and LLM both returned nothing.

        Strict guards:
        - Only when exactly 2 canonical nodes co-occur (no combinatorial explosion)
        - Only 1 predicate signal (the strongest match)
        - Lower confidence than regex/LLM
        """
        canonical_nodes = segment.get("canonical_nodes") or []
        if len(canonical_nodes) != 2:
            return []

        text = segment.get("normalized_text") or segment.get("raw_text", "")
        detected = self._detect_predicates(text)
        if not detected:
            return []

        ontology = self._ontology
        predicate = detected[0]  # only the single strongest signal
        if not ontology.is_valid_relation(predicate):
            return []

        subj_id, obj_id = canonical_nodes[0], canonical_nodes[1]
        if subj_id == obj_id:
            return []

        return [self._build_fact(
            subj_id, predicate, obj_id, segment, source_rank, "cooccurrence",
        )]

    def _build_fact(
        self, subj_id: str, predicate: str, obj_id: str,
        segment: dict, source_rank: str, extraction_method: str,
    ) -> dict:
        conf = score_fact(
            source_rank=source_rank,
            extraction_method="rule" if extraction_method != "llm" else "llm",
            ontology_fit=0.85 if extraction_method == "rule" else 0.60,
            cross_source_consistency=0.5,
            temporal_validity=1.0,
        )
        return {
            "fact_id":           str(uuid.uuid4()),
            "subject":           subj_id,
            "predicate":         predicate,
            "object":            obj_id,
            "qualifier":         {},
            "domain":            subj_id.split(".")[0] if "." in subj_id else None,
            "confidence":        conf,
            "extraction_method": extraction_method,
            "segment_id":        segment["segment_id"],
            "source_rank":       source_rank,
            "lifecycle_state":   "active",
            "ontology_version":  "v0.2.0",
        }

    def _detect_predicates(self, text: str) -> list[str]:
        """Detect which relation predicates are signaled by keywords in text."""
        predicates = []
        text_sample = text[:3000]
        for pattern, predicate in self._predicate_signals:
            if pattern.search(text_sample):
                predicates.append(predicate)
        return predicates

    def extract_facts_llm(self, segment: dict, source_rank: str) -> list[dict]:
        """LLM-based extraction: uses segment's canonical tags as node context."""
        llm = self._llm
        ontology = self._ontology
        if not llm.is_enabled():
            return []

        text = segment.get("normalized_text") or segment.get("raw_text", "")
        if not text.strip():
            return []

        canonical_nodes = segment.get("canonical_nodes") or []
        candidate_ids = [n for n in canonical_nodes if n]

        # Always include mechanism/method/condition/scenario nodes so LLM
        # can extract cross-layer relationships, not just concept-level facts
        multi_layer_ids = [
            nid for nid in ontology.all_node_ids()
            if nid.startswith(("MECH.", "METHOD.", "COND.", "SCENE."))
        ]
        candidate_ids = list(set(candidate_ids + multi_layer_ids))

        if len(candidate_ids) < 5:
            candidate_ids = ontology.all_node_ids()[:100]

        valid_relations = list(ontology.relation_ids)

        raw_triples = llm.extract_triples(text, candidate_ids, valid_relations)
        facts: list[dict] = []
        for triple in raw_triples:
            subj = triple.get("subject", "")
            pred = triple.get("predicate", "")
            obj = triple.get("object", "")
            if not subj or not pred or not obj or subj == obj:
                continue
            # Normalize subject/object: try alias → node_id mapping
            subj = ontology.resolve_alias(subj) or subj
            obj = ontology.resolve_alias(obj) or obj
            # Normalize predicate: lowercase, strip, underscores
            pred = pred.strip().lower().replace(" ", "_").replace("-", "_")
            if not ontology.is_valid_relation(pred):
                # Unknown predicate → candidate relation pool
                self._record_relation_candidate(
                    pred, subj, obj, segment, source_doc_id=segment.get("source_doc_id", ""),
                )
                continue
            conf = score_fact(
                source_rank=source_rank,
                extraction_method="llm",
                ontology_fit=0.75,
                cross_source_consistency=0.5,
                temporal_validity=1.0,
            )
            facts.append({
                "fact_id":           str(uuid.uuid4()),
                "subject":           subj,
                "predicate":         pred,
                "object":            obj,
                "qualifier":         {},
                "domain":            subj.split(".")[0] if "." in subj else None,
                "confidence":        conf,
                "extraction_method": "llm",
                "segment_id":        segment["segment_id"],
                "source_rank":       source_rank,
                "lifecycle_state":   "active",
                "ontology_version":  "v0.2.0",
            })
        return facts

    def _record_relation_candidate(
        self, predicate: str, subject: str, obj: str,
        segment: dict, source_doc_id: str,
    ) -> None:
        """Store an unknown predicate into evolution_candidates (type='relation')."""
        import json
        from src.utils.normalize import normalize_term
        store = self._store
        normalized = normalize_term(predicate)
        example = json.dumps([{
            "subject": subject, "object": obj,
            "segment_id": str(segment.get("segment_id", "")),
            "source_doc_id": source_doc_id,
        }])
        try:
            store.execute(
                """
                INSERT INTO governance.evolution_candidates
                    (surface_forms, normalized_form, candidate_type, examples,
                     source_count, first_seen_at, last_seen_at, review_status)
                VALUES (ARRAY[%s], %s, 'relation', %s::jsonb, 1, NOW(), NOW(), 'discovered')
                ON CONFLICT (normalized_form) DO UPDATE SET
                    source_count = governance.evolution_candidates.source_count + 1,
                    last_seen_at = NOW(),
                    examples = governance.evolution_candidates.examples || %s::jsonb
                """,
                (predicate, normalized, example, example),
            )
            log.debug("  relation candidate: %s (%s → %s)", predicate, subject, obj)
        except Exception as exc:
            log.warning("Failed to record relation candidate %s: %s", predicate, exc)

    def _resolve_term(self, term: str) -> str | None:
        return self._ontology.lookup_alias(term.lower())

    def _save_facts(self, facts: list[dict], source_doc_id: str) -> None:
        if not facts:
            return
        store = self._store
        with store.transaction() as cur:
            for f in facts:
                cur.execute(
                    """
                    INSERT INTO facts (fact_id, subject, predicate, object, qualifier,
                        domain, confidence, lifecycle_state, ontology_version)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        f["fact_id"], f["subject"], f["predicate"], f["object"],
                        "{}", f.get("domain"), f["confidence"],
                        f["lifecycle_state"], f["ontology_version"],
                    ),
                )
                cur.execute(
                    """
                    INSERT INTO evidence (evidence_id, fact_id, source_doc_id, segment_id,
                        source_rank, extraction_method, evidence_score)
                    VALUES (%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(uuid.uuid4()), f["fact_id"], source_doc_id,
                        f.get("segment_id"), f["source_rank"],
                        f["extraction_method"], f["confidence"],
                    ),
                )
        log.info("Saved facts doc=%s facts=%d evidence=%d", source_doc_id, len(facts), len(facts))

    @staticmethod
    def _preview_ids(values: list[str], limit: int = 8) -> str:
        if not values:
            return "[]"
        if len(values) <= limit:
            return "[" + ", ".join(values) + "]"
        return "[" + ", ".join(values[:limit]) + f", ...(+{len(values) - limit})]"