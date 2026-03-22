"""Stage 4: Relation extraction + fact construction — rules R1-R4."""

from __future__ import annotations

import logging
import re
import uuid

from src.db.postgres import fetchall, execute, get_conn
from src.ontology.registry import OntologyRegistry
from src.utils.confidence import score_fact

log = logging.getLogger(__name__)

# Rule R2: rule-based relation patterns (pattern, predicate)
RELATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'(\b[\w\-]+)\s+uses?\s+(?:the\s+)?(\b[\w\-]+)\s+protocol', re.I), "uses_protocol"),
    (re.compile(r'(\b[\w\-]+)\s+is\s+(?:a\s+type\s+of|a\s+kind\s+of|an?\s+)(\b[\w\-]+)', re.I), "is_a"),
    (re.compile(r'(\b[\w\-]+)\s+(?:is\s+)?part\s+of\s+(\b[\w\-]+)', re.I), "part_of"),
    (re.compile(r'(\b[\w\-]+)\s+depends?\s+on\s+(\b[\w\-]+)', re.I), "depends_on"),
    (re.compile(r'(\b[\w\-]+)\s+requires?\s+(\b[\w\-]+)', re.I), "requires"),
    (re.compile(r'(\b[\w\-]+)\s+encapsulates?\s+(\b[\w\-]+)', re.I), "encapsulates"),
    (re.compile(r'(\b[\w\-]+)\s+establishes?\s+(?:a\s+)?(\b[\w\-]+)', re.I), "establishes"),
    (re.compile(r'(\b[\w\-]+)\s+advertises?\s+(\b[\w\-]+)', re.I), "advertises"),
    (re.compile(r'(\b[\w\-]+)\s+impacts?\s+(\b[\w\-]+)', re.I), "impacts"),
    (re.compile(r'(\b[\w\-]+)\s+causes?\s+(\b[\w\-]+)', re.I), "causes"),
    (re.compile(r'(\b[\w\-]+)\s+(?:is\s+)?implemented\s+(?:by|on)\s+(\b[\w\-]+)', re.I), "implements"),
    (re.compile(r'(\b[\w\-]+)\s+forwards?\s+(?:traffic\s+)?(?:via|through)\s+(\b[\w\-]+)', re.I), "forwards_via"),
    (re.compile(r'(\b[\w\-]+)\s+protects?\s+(\b[\w\-]+)', re.I), "protects"),
    (re.compile(r'(\b[\w\-]+)\s+monitors?\s+(\b[\w\-]+)', re.I), "monitored_by"),
    (re.compile(r'(\b[\w\-]+)\s+(?:is\s+)?configured\s+(?:by|via|using)\s+(\b[\w\-]+)', re.I), "configured_by"),
]


class ExtractStage:
    def __init__(self) -> None:
        self.registry = OntologyRegistry.from_default()

    def process(self, source_doc_id: str) -> list[dict]:
        """Extract facts from all segments of a document."""
        doc = fetchall(
            "SELECT source_rank FROM documents WHERE source_doc_id=%s", (source_doc_id,)
        )
        source_rank = doc[0]["source_rank"] if doc else "C"

        segments = fetchall(
            """
            SELECT s.*, array_agg(st.ontology_node_id) FILTER (WHERE st.tag_type='canonical') as canonical_nodes
            FROM segments s
            LEFT JOIN segment_tags st ON s.segment_id = st.segment_id
            WHERE s.source_doc_id=%s AND s.lifecycle_state='active'
            GROUP BY s.segment_id
            """,
            (source_doc_id,),
        )

        all_facts: list[dict] = []
        for seg in segments:
            facts = self.extract_facts(seg, source_rank)
            all_facts.extend(facts)

        self._save_facts(all_facts, source_doc_id)
        execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) VALUES ('dedup',%s,'pending','0.1.0')",
            (source_doc_id,),
        )
        log.info("Extracted %d facts from doc %s", len(all_facts), source_doc_id)
        return all_facts

    def extract_facts(self, segment: dict, source_rank: str) -> list[dict]:
        """Rule R1-R4: apply patterns, validate, score."""
        text = segment.get("normalized_text") or segment.get("raw_text", "")
        facts: list[dict] = []

        for pattern, predicate in RELATION_PATTERNS:
            # Rule R1: predicate must be in controlled set
            if not self.registry.is_valid_relation(predicate):
                continue

            for m in pattern.finditer(text):
                subj_raw = m.group(1).strip()
                obj_raw  = m.group(2).strip()

                subj_id = self._resolve_term(subj_raw)
                obj_id  = self._resolve_term(obj_raw)

                # Rule R1: both endpoints must resolve to ontology nodes
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
                    "ontology_version":  "v0.1.0",
                })

        return facts

    def _resolve_term(self, term: str) -> str | None:
        return self.registry.lookup_alias(term.lower())

    def _save_facts(self, facts: list[dict], source_doc_id: str) -> None:
        if not facts:
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
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
