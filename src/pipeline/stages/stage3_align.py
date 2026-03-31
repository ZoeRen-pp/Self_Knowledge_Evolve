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

_CONTEXT_PATTERNS = [
    (re.compile(r"\bdata center\b|\bdc fabric\b", re.I), "数据中心"),
    (re.compile(r"\bcampus\b|\benterprise\b", re.I), "园区网"),
    (re.compile(r"\bcarrier\b|\bservice provider\b|\bsp network\b", re.I), "承载网"),
    (re.compile(r"\baccess network\b|\bpon\b|\bolt\b", re.I), "接入网"),
    (re.compile(r"\b5gc\b|\b5g core\b|\bamf\b|\bsmf\b", re.I), "5GC"),
    (re.compile(r"\bmulti.vendor\b|\binterop\b", re.I), "多厂商组网"),
]

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

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._ontology = app.ontology
        self._store = app.store
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
        total_tags = 0
        pending = 0
        candidate_terms = 0

        for seg in segments:
            tags, candidates = self.align_segment(seg)
            total_tags += self._save_tags(seg["segment_id"], tags)
            candidate_terms += candidates

            canonical_count = sum(1 for t in tags if t["tag_type"] == "canonical")
            if canonical_count == 0:
                store.execute(
                    "UPDATE segments SET lifecycle_state='pending_alignment' WHERE segment_id=%s",
                    (seg["segment_id"],),
                )
                pending += 1

        store.execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) "
            "VALUES ('relation_extraction',%s,'pending','0.2.0')",
            (source_doc_id,),
        )
        log.info(
            "Aligned doc=%s tags=%d pending_segments=%d candidates_seen=%d",
            source_doc_id,
            total_tags,
            pending,
            candidate_terms,
        )

    def align_segment(self, segment: dict) -> tuple[list[dict], int]:
        """Rules A1-A5: produce canonical + semantic_role + context tags."""
        text = segment.get("normalized_text") or segment.get("raw_text", "")
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

        candidate_terms = self._collect_candidates(text, matched_nodes, segment["source_doc_id"])

        seg_type = segment.get("segment_type", "unknown")
        if seg_type in _SEMANTIC_ROLE_TAGS:
            tags.append({
                "tag_type":        "semantic_role",
                "tag_value":       _SEMANTIC_ROLE_TAGS[seg_type],
                "ontology_node_id": None,
                "confidence":      1.0,
                "tagger":          "rule",
            })

        for pattern, ctx in _CONTEXT_PATTERNS:
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

    def _collect_candidates(
        self, text: str, matched_nodes: dict, source_doc_id: str
    ) -> int:
        """Rule A3: terms that don't match ontology -> candidate pool.

        Uses normalized_form as dedup key; accumulates source_count and
        seen_source_doc_ids across documents.
        """
        from src.utils.normalize import normalize_term
        ontology = self._ontology
        store = self._store
        candidates = re.findall(r"\b([A-Z][A-Za-z0-9\-]{2,}|[A-Z]{2,10})\b", text)
        unmatched = [
            c for c in candidates
            if not ontology.lookup_alias(c.lower())
        ]
        for term in set(unmatched):
            normalized = normalize_term(term)
            store.execute(
                """
                INSERT INTO evolution_candidates
                    (surface_forms, normalized_form, source_count, last_seen_at,
                     first_seen_at, seen_source_doc_ids, review_status)
                VALUES (ARRAY[%s], %s, 1, NOW(), NOW(), ARRAY[%s::uuid], 'discovered')
                ON CONFLICT (normalized_form) DO UPDATE SET
                    source_count = evolution_candidates.source_count + 1,
                    last_seen_at = NOW(),
                    surface_forms = CASE
                        WHEN NOT (%s = ANY(evolution_candidates.surface_forms))
                        THEN array_append(evolution_candidates.surface_forms, %s)
                        ELSE evolution_candidates.surface_forms
                    END,
                    seen_source_doc_ids = CASE
                        WHEN NOT (%s::uuid = ANY(evolution_candidates.seen_source_doc_ids))
                        THEN array_append(evolution_candidates.seen_source_doc_ids, %s::uuid)
                        ELSE evolution_candidates.seen_source_doc_ids
                    END
                """,
                (term, normalized, source_doc_id, term, term, source_doc_id, source_doc_id),
            )
        return len(set(unmatched))

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