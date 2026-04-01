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

RELATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(\b[\w\-]+)\s+uses?\s+(?:the\s+)?(\b[\w\-]+)\s+protocol", re.I), "uses_protocol"),
    (re.compile(r"(\b[\w\-]+)\s+is\s+(?:a\s+type\s+of|a\s+kind\s+of|an?\s+)(\b[\w\-]+)", re.I), "is_a"),
    (re.compile(r"(\b[\w\-]+)\s+(?:is\s+)?part\s+of\s+(\b[\w\-]+)", re.I), "part_of"),
    (re.compile(r"(\b[\w\-]+)\s+depends?\s+on\s+(\b[\w\-]+)", re.I), "depends_on"),
    (re.compile(r"(\b[\w\-]+)\s+requires?\s+(\b[\w\-]+)", re.I), "requires"),
    (re.compile(r"(\b[\w\-]+)\s+encapsulates?\s+(\b[\w\-]+)", re.I), "encapsulates"),
    (re.compile(r"(\b[\w\-]+)\s+establishes?\s+(?:a\s+)?(\b[\w\-]+)", re.I), "establishes"),
    (re.compile(r"(\b[\w\-]+)\s+advertises?\s+(\b[\w\-]+)", re.I), "advertises"),
    (re.compile(r"(\b[\w\-]+)\s+impacts?\s+(\b[\w\-]+)", re.I), "impacts"),
    (re.compile(r"(\b[\w\-]+)\s+causes?\s+(\b[\w\-]+)", re.I), "causes"),
    (re.compile(r"(\b[\w\-]+)\s+(?:is\s+)?implemented\s+(?:by|on)\s+(\b[\w\-]+)", re.I), "implements"),
    (re.compile(r"(\b[\w\-]+)\s+forwards?\s+(?:traffic\s+)?(?:via|through)\s+(\b[\w\-]+)", re.I), "forwards_via"),
    (re.compile(r"(\b[\w\-]+)\s+protects?\s+(\b[\w\-]+)", re.I), "protects"),
    (re.compile(r"(\b[\w\-]+)\s+monitors?\s+(\b[\w\-]+)", re.I), "monitored_by"),
    (re.compile(r"(\b[\w\-]+)\s+(?:is\s+)?configured\s+(?:by|via|using)\s+(\b[\w\-]+)", re.I), "configured_by"),
]


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

        all_facts: list[dict] = []
        rule_count = 0
        llm_count = 0

        for seg in segments:
            facts = self.extract_facts(seg, source_rank)
            all_facts.extend(facts)
            rule_count += len(facts)
            llm_facts = self.extract_facts_llm(seg, source_rank)
            all_facts.extend(llm_facts)
            llm_count += len(llm_facts)

        self._save_facts(all_facts, source_doc_id)
        self._crawler_store.execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) "
            "VALUES ('dedup',%s,'pending','0.2.0')",
            (source_doc_id,),
        )
        fact_ids = [f["fact_id"] for f in all_facts]
        id_preview = self._preview_ids(fact_ids)
        log.info(
            "Extracted facts doc=%s total=%d rule=%d llm=%d fact_ids=%s",
            source_doc_id,
            len(all_facts),
            rule_count,
            llm_count,
            id_preview,
        )
        return all_facts

    def extract_facts(self, segment: dict, source_rank: str) -> list[dict]:
        """Rule R1-R4: apply patterns, validate, score."""
        text = segment.get("normalized_text") or segment.get("raw_text", "")
        ontology = self._ontology
        facts: list[dict] = []

        for pattern, predicate in RELATION_PATTERNS:
            if not ontology.is_valid_relation(predicate):
                continue

            for m in pattern.finditer(text):
                subj_raw = m.group(1).strip()
                obj_raw = m.group(2).strip()

                subj_id = self._resolve_term(subj_raw)
                obj_id = self._resolve_term(obj_raw)

                if not subj_id or not obj_id:
                    continue
                if subj_id == obj_id:
                    continue

                conf = score_fact(
                    source_rank=source_rank,
                    extraction_method="rule",
                    ontology_fit=0.85,
                    cross_source_consistency=0.5,
                    temporal_validity=1.0,
                )

                facts.append({
                    "fact_id":           str(uuid.uuid4()),
                    "subject":           subj_id,
                    "predicate":         predicate,
                    "object":            obj_id,
                    "qualifier":         {},
                    "domain":            subj_id.split(".")[0] if "." in subj_id else None,
                    "confidence":        conf,
                    "extraction_method": "rule",
                    "segment_id":        segment["segment_id"],
                    "source_rank":       source_rank,
                    "lifecycle_state":   "active",
                    "ontology_version":  "v0.2.0",
                })

        return facts

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
            if not ontology.is_valid_relation(pred):
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