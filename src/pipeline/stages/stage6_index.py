"""Stage 6: Index into Neo4j graph — rules I1-I3 + embedding write."""

from __future__ import annotations

import logging

from semcore.core.context import PipelineContext
from semcore.pipeline.base import Stage
from semcore.providers.base import GraphStore, RelationalStore

from src.config.settings import settings
from src.utils.embedding import get_embeddings, vector_to_pg_literal

log = logging.getLogger(__name__)

# Confidence gate thresholds (Rule I1)
MIN_SEGMENT_CONFIDENCE = 0.5
MIN_FACT_CONFIDENCE    = 0.5

# Five-layer tag types that link segments to non-concept ontology nodes
_LAYER_TAG_TYPES = {"canonical", "mechanism_tag", "method_tag", "condition_tag", "scenario_tag"}

# Neo4j label per knowledge layer
_LAYER_NEO4J_LABEL = {
    "concept":   "OntologyNode",
    "mechanism": "MechanismNode",
    "method":    "MethodNode",
    "condition": "ConditionRuleNode",
    "scenario":  "ScenarioPatternNode",
}


class IndexStage(Stage):
    name = "index"

    def __init__(self) -> None:
        self._store: RelationalStore | None = None
        self._graph: GraphStore | None = None

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._store = app.store
        self._graph = app.graph
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        stats = self._run(source_doc_id)
        self.set_output(ctx, stats)
        return ctx

    def _run(self, source_doc_id: str) -> dict:
        """Index document, segments, facts, evidence into Neo4j."""
        store = self._store
        stats = {"segments_indexed": 0, "facts_indexed": 0, "evidence_indexed": 0}

        # Load from PG
        doc = store.fetchall("SELECT * FROM documents WHERE source_doc_id=%s", (source_doc_id,))
        if not doc:
            log.error("Document %s not found", source_doc_id)
            return stats
        doc = doc[0]

        segments = store.fetchall(
            "SELECT * FROM segments WHERE source_doc_id=%s AND lifecycle_state='active' AND confidence>=%s",
            (source_doc_id, MIN_SEGMENT_CONFIDENCE),
        )
        facts = store.fetchall(
            """
            SELECT DISTINCT f.* FROM facts f
            JOIN evidence e ON f.fact_id = e.fact_id
            WHERE e.source_doc_id=%s AND f.lifecycle_state='active' AND f.confidence>=%s
            """,
            (source_doc_id, MIN_FACT_CONFIDENCE),
        )
        evidence = store.fetchall(
            "SELECT * FROM evidence WHERE source_doc_id=%s",
            (source_doc_id,),
        )
        seg_tags = store.fetchall(
            """
            SELECT st.* FROM segment_tags st
            JOIN segments s ON st.segment_id = s.segment_id
            WHERE s.source_doc_id=%s AND st.tag_type = ANY(%s)
            """,
            (source_doc_id, list(_LAYER_TAG_TYPES)),
        )

        # Rule I2: write order — PG already done, now Neo4j
        self._index_document(doc)
        stats["segments_indexed"] = self._index_segments(segments)
        stats["facts_indexed"]    = self._index_facts(facts)
        stats["evidence_indexed"] = self._index_evidence(evidence, facts)
        self._index_tags(seg_tags)
        self._write_embeddings(segments)
        self._write_edu_embeddings(segments)

        # Rule I3: mark as indexed in PG
        store.execute(
            "UPDATE documents SET status='indexed' WHERE source_doc_id=%s", (source_doc_id,)
        )
        log.info("Indexed doc %s → %s", source_doc_id, stats)
        return stats

    # ── Neo4j writers ─────────────────────────────────────────────

    def _index_document(self, doc: dict) -> None:
        self._graph.write(
            """
            MERGE (d:SourceDocument {source_doc_id: $source_doc_id})
            SET d.canonical_url  = $canonical_url,
                d.title          = $title,
                d.site_key       = $site_key,
                d.source_rank    = $source_rank,
                d.doc_type       = $doc_type,
                d.language       = $language,
                d.crawl_time     = $crawl_time,
                d.lifecycle_state = 'active'
            """,
            source_doc_id=str(doc["source_doc_id"]),
            canonical_url=doc.get("canonical_url") or "",
            title=doc.get("title") or "",
            site_key=doc.get("site_key") or "",
            source_rank=doc.get("source_rank") or "C",
            doc_type=doc.get("doc_type") or "unknown",
            language=doc.get("language") or "en",
            crawl_time=str(doc.get("crawl_time") or ""),
        )

    def _index_segments(self, segments: list[dict]) -> int:
        count = 0
        for seg in segments:
            # Build a display name: prefer section_title, fallback to segment_type + index
            section_title = seg.get("section_title") or ""
            seg_type = seg.get("segment_type") or "unknown"
            name = section_title if section_title else f"{seg_type}#{seg.get('segment_index', count)}"

            self._graph.write(
                """
                MERGE (s:KnowledgeSegment {segment_id: $segment_id})
                SET s.name           = $name,
                    s.source_doc_id  = $source_doc_id,
                    s.segment_type   = $segment_type,
                    s.section_title  = $section_title,
                    s.token_count    = $token_count,
                    s.confidence     = $confidence,
                    s.lifecycle_state = 'active',
                    s.ontology_version = $ontology_version
                WITH s
                MATCH (d:SourceDocument {source_doc_id: $source_doc_id})
                MERGE (s)-[:BELONGS_TO]->(d)
                """,
                name=name,
                segment_id=str(seg["segment_id"]),
                source_doc_id=str(seg["source_doc_id"]),
                segment_type=seg_type,
                section_title=section_title,
                token_count=seg.get("token_count") or 0,
                confidence=float(seg.get("confidence") or 0.5),
                ontology_version=settings.ONTOLOGY_VERSION,
            )
            count += 1
        return count

    def _index_facts(self, facts: list[dict]) -> int:
        count = 0
        for f in facts:
            self._graph.write(
                """
                MERGE (f:Fact {fact_id: $fact_id})
                SET f.subject          = $subject,
                    f.predicate        = $predicate,
                    f.object           = $object,
                    f.domain           = $domain,
                    f.confidence       = $confidence,
                    f.lifecycle_state  = $lifecycle_state,
                    f.ontology_version = $ontology_version
                WITH f
                MATCH (a:OntologyNode {node_id: $subject})
                MATCH (b:OntologyNode {node_id: $object})
                MERGE (a)-[r:RELATED_TO {predicate: $predicate, fact_id: $fact_id}]->(b)
                SET r.confidence = $confidence
                """,
                fact_id=str(f["fact_id"]),
                subject=f["subject"],
                predicate=f["predicate"],
                object=f["object"],
                domain=f.get("domain") or "",
                confidence=float(f.get("confidence") or 0.5),
                lifecycle_state=f.get("lifecycle_state") or "active",
                ontology_version=settings.ONTOLOGY_VERSION,
            )
            count += 1
        return count

    def _index_evidence(self, evidence: list[dict], facts: list[dict]) -> int:
        fact_ids = {str(f["fact_id"]) for f in facts}
        count = 0
        for ev in evidence:
            if str(ev["fact_id"]) not in fact_ids:
                continue
            self._graph.write(
                """
                MERGE (e:Evidence {evidence_id: $evidence_id})
                SET e.source_rank       = $source_rank,
                    e.extraction_method = $extraction_method,
                    e.evidence_score    = $evidence_score,
                    e.exact_span        = $exact_span
                WITH e
                MATCH (f:Fact {fact_id: $fact_id})
                MERGE (f)-[:SUPPORTED_BY {evidence_score: $evidence_score}]->(e)
                WITH e
                MATCH (s:KnowledgeSegment {segment_id: $segment_id})
                MERGE (e)-[:EXTRACTED_FROM]->(s)
                """,
                evidence_id=str(ev["evidence_id"]),
                fact_id=str(ev["fact_id"]),
                segment_id=str(ev.get("segment_id") or ""),
                source_rank=ev.get("source_rank") or "C",
                extraction_method=ev.get("extraction_method") or "rule",
                evidence_score=float(ev.get("evidence_score") or 0.5),
                exact_span=ev.get("exact_span") or "",
            )
            count += 1
        return count

    def _index_tags(self, seg_tags: list[dict]) -> None:
        for tag in seg_tags:
            if not tag.get("ontology_node_id"):
                continue
            # Determine the target Neo4j label from the tag's ontology node prefix
            node_id = tag["ontology_node_id"]
            prefix = node_id.split(".")[0] if "." in node_id else ""
            _prefix_to_layer = {
                "MECH":  "mechanism",
                "METHOD": "method",
                "COND":  "condition",
                "SCENE": "scenario",
            }
            layer = _prefix_to_layer.get(prefix.upper(), "concept")
            neo4j_label = _LAYER_NEO4J_LABEL.get(layer, "OntologyNode")

            self._graph.write(
                f"""
                MATCH (s:KnowledgeSegment {{segment_id: $segment_id}})
                MERGE (n:{neo4j_label} {{node_id: $node_id}})
                MERGE (s)-[r:TAGGED_WITH]->(n)
                SET r.confidence = $confidence,
                    r.tagger     = $tagger,
                    r.tag_type   = $tag_type
                """,
                segment_id=str(tag["segment_id"]),
                node_id=node_id,
                confidence=float(tag.get("confidence") or 0.8),
                tagger=tag.get("tagger") or "rule",
                tag_type=tag.get("tag_type") or "canonical",
            )

    def _write_embeddings(self, segments: list[dict]) -> None:
        """Generate embeddings for segments and store in PG (best-effort)."""
        if not segments:
            return
        texts = [
            (seg["segment_id"], seg.get("normalized_text") or seg.get("raw_text", ""))
            for seg in segments
        ]
        texts = [(sid, t) for sid, t in texts if t.strip()]
        if not texts:
            return

        segment_ids = [sid for sid, _ in texts]
        raw_texts   = [t   for _, t in texts]

        vecs = get_embeddings(raw_texts)
        if vecs is None:
            return  # embedding disabled or model unavailable

        store = self._store
        with store.transaction() as cur:
            for seg_id, vec in zip(segment_ids, vecs):
                pg_vec = vector_to_pg_literal(vec)
                cur.execute(
                    "UPDATE segments SET embedding = %s::vector WHERE segment_id = %s",
                    (pg_vec, str(seg_id)),
                )
        log.info("Wrote embeddings for %d segments", len(segment_ids))

    def _write_edu_embeddings(self, segments: list[dict]) -> None:
        """Generate title_vec + content_vec for segments (best-effort)."""
        if not segments:
            return

        store = self._store
        seg_ids  = [str(seg["segment_id"]) for seg in segments]
        contents = [seg.get("normalized_text") or seg.get("raw_text", "") for seg in segments]

        # Fetch titles from segments (written by stage2)
        rows = store.fetchall(
            "SELECT segment_id, title FROM segments WHERE segment_id = ANY(%s)",
            (seg_ids,),
        )
        title_map = {str(r["segment_id"]): r.get("title") or "" for r in rows}
        titles = [title_map.get(sid, "") for sid in seg_ids]

        content_vecs = get_embeddings(contents)
        title_vecs   = get_embeddings(titles) if any(titles) else None

        if content_vecs is None:
            return

        with store.transaction() as cur:
            for i, seg_id in enumerate(seg_ids):
                c_vec = vector_to_pg_literal(content_vecs[i])
                t_vec = vector_to_pg_literal(title_vecs[i]) if title_vecs else None
                cur.execute(
                    """
                    UPDATE segments
                    SET content_vec = %s::vector,
                        title_vec   = CASE WHEN %s IS NOT NULL THEN %s::vector ELSE title_vec END
                    WHERE segment_id = %s
                    """,
                    (c_vec, t_vec, t_vec, seg_id),
                )
        log.info("Wrote EDU embeddings for %d segments", len(seg_ids))