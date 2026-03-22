"""Stage 3: Ontology alignment + tagging — rules A1-A5."""

from __future__ import annotations

import logging
import re
import uuid

from src.db.postgres import fetchall, execute, get_conn
from src.ontology.registry import OntologyRegistry

log = logging.getLogger(__name__)

_SEMANTIC_ROLE_TAGS = {
    "definition": "定义", "mechanism": "机制", "constraint": "约束",
    "config": "配置", "fault": "故障", "troubleshooting": "排障",
    "best_practice": "最佳实践", "performance": "性能",
    "comparison": "对比", "table": "表格", "code": "配置",
}

# Context keywords → context tags
_CONTEXT_PATTERNS = [
    (re.compile(r'\bdata center\b|\bdc fabric\b', re.I), "数据中心"),
    (re.compile(r'\bcampus\b|\benterprise\b', re.I), "园区网"),
    (re.compile(r'\bcarrier\b|\bservice provider\b|\bsp network\b', re.I), "承载网"),
    (re.compile(r'\baccess network\b|\bpon\b|\bolt\b', re.I), "接入网"),
    (re.compile(r'\b5gc\b|\b5g core\b|\bamf\b|\bsmf\b', re.I), "5GC"),
    (re.compile(r'\bmulti.vendor\b|\binterop\b', re.I), "多厂商组网"),
]


class AlignStage:
    def __init__(self) -> None:
        self.registry = OntologyRegistry.from_default()

    def process(self, source_doc_id: str) -> None:
        """Align all segments for a document; insert segment_tags."""
        segments = fetchall(
            "SELECT * FROM segments WHERE source_doc_id=%s AND lifecycle_state='active'",
            (source_doc_id,),
        )
        for seg in segments:
            tags = self.align_segment(seg)
            self._save_tags(seg["segment_id"], tags, source_doc_id)

            canonical_count = sum(1 for t in tags if t["tag_type"] == "canonical")
            if canonical_count == 0:
                execute(
                    "UPDATE segments SET lifecycle_state='pending_alignment' WHERE segment_id=%s",
                    (seg["segment_id"],),
                )

        execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) VALUES ('relation_extraction',%s,'pending','0.1.0')",
            (source_doc_id,),
        )
        log.info("Aligned segments for doc %s", source_doc_id)

    def align_segment(self, segment: dict) -> list[dict]:
        """Rules A1-A5: produce canonical + semantic_role + context tags."""
        text = segment.get("normalized_text") or segment.get("raw_text", "")
        tags: list[dict] = []

        # Rule A1 & A2: canonical tags via alias/node lookup
        matched_nodes: dict[str, float] = {}  # node_id → confidence
        for surface, node_id, conf in self._find_terms(text):
            if node_id not in matched_nodes or matched_nodes[node_id] < conf:
                matched_nodes[node_id] = conf
                # Rule A4: vendor terms → add qualifier (handled via confidence demotion)

        for node_id, conf in matched_nodes.items():
            node = self.registry.get_node(node_id)
            tags.append({
                "tag_type":        "canonical",
                "tag_value":       node["canonical_name"] if node else node_id,
                "ontology_node_id": node_id,
                "confidence":      conf,
                "tagger":          "rule",
            })

        # Rule A3: collect unmatched high-frequency terms → candidate pool
        self._collect_candidates(text, matched_nodes, segment["source_doc_id"])

        # Semantic role tag
        seg_type = segment.get("segment_type", "unknown")
        if seg_type in _SEMANTIC_ROLE_TAGS:
            tags.append({
                "tag_type":        "semantic_role",
                "tag_value":       _SEMANTIC_ROLE_TAGS[seg_type],
                "ontology_node_id": None,
                "confidence":      1.0,
                "tagger":          "rule",
            })

        # Context tags
        for pattern, ctx in _CONTEXT_PATTERNS:
            if pattern.search(text[:1000]):
                tags.append({
                    "tag_type":        "context",
                    "tag_value":       ctx,
                    "ontology_node_id": None,
                    "confidence":      0.85,
                    "tagger":          "rule",
                })

        return tags

    # ── Private ───────────────────────────────────────────────────

    def _find_terms(self, text: str) -> list[tuple[str, str, float]]:
        """Rule A1: exact & alias match. Returns [(surface_form, node_id, confidence)]."""
        found: list[tuple[str, str, float]] = []
        text_lower = text.lower()

        for surface, node_id in self.registry.alias_map.items():
            if surface in text_lower:
                # Prefer exact node name matches (higher confidence)
                node = self.registry.get_node(node_id)
                if node and node.get("canonical_name", "").lower() == surface:
                    found.append((surface, node_id, 1.0))
                else:
                    found.append((surface, node_id, 0.90))

        return found

    def _collect_candidates(
        self, text: str, matched_nodes: dict, source_doc_id: str
    ) -> None:
        """Rule A3: terms that don't match ontology → candidate pool."""
        # Simple heuristic: ALL_CAPS or Title-Case tokens not in ontology
        candidates = re.findall(r'\b([A-Z][A-Za-z0-9\-]{2,}|[A-Z]{2,10})\b', text)
        unmatched = [
            c for c in candidates
            if not self.registry.lookup_alias(c.lower())
        ]
        for term in set(unmatched):
            execute(
                """
                INSERT INTO evolution_candidates (surface_forms, source_count, last_seen_at)
                VALUES (ARRAY[%s], 1, NOW())
                ON CONFLICT DO NOTHING
                """,
                (term,),
            )

    def _save_tags(self, segment_id: str, tags: list[dict], source_doc_id: str) -> None:
        if not tags:
            return
        with get_conn() as conn:
            with conn.cursor() as cur:
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
