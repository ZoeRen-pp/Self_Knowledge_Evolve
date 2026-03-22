"""Stage 6: Index into Neo4j graph — rules I1-I3."""

from __future__ import annotations

import logging

from src.db.postgres import fetchall, execute
from src.db.neo4j_client import run_write
from src.config.settings import settings

log = logging.getLogger(__name__)

# Confidence gate thresholds (Rule I1)
MIN_SEGMENT_CONFIDENCE = 0.5
MIN_FACT_CONFIDENCE    = 0.5


class IndexStage:
    def process(self, source_doc_id: str) -> dict:
        """Index document, segments, facts, evidence into Neo4j."""
        stats = {"segments_indexed": 0, "facts_indexed": 0, "evidence_indexed": 0}

        # Load from PG
        doc = fetchall("SELECT * FROM documents WHERE source_doc_id=%s", (source_doc_id,))
        if not doc:
            log.error("Document %s not found", source_doc_id)
            return stats
        doc = doc[0]

        segments = fetchall(
            "SELECT * FROM segments WHERE source_doc_id=%s AND lifecycle_state='active' AND confidence>=%s",
            (source_doc_id, MIN_SEGMENT_CONFIDENCE),
        )
        facts = fetchall(
            """
            SELECT DISTINCT f.* FROM facts f
            JOIN evidence e ON f.fact_id = e.fact_id
            WHERE e.source_doc_id=%s AND f.lifecycle_state='active' AND f.confidence>=%s
            """,
            (source_doc_id, MIN_FACT_CONFIDENCE),
        )
        evidence = fetchall(
            "SELECT * FROM evidence WHERE source_doc_id=%s",
            (source_doc_id,),
        )
        seg_tags = fetchall(
            """
            SELECT st.* FROM segment_tags st
            JOIN segments s ON st.segment_id = s.segment_id
            WHERE s.source_doc_id=%s AND st.tag_type='canonical'
            """,
            (source_doc_id,),
        )

        # Rule I2: write order — PG already done, now Neo4j
        self._index_document(doc)
        stats["segments_indexed"] = self._index_segments(segments)
        stats["facts_indexed"]    = self._index_facts(facts)
        stats["evidence_indexed"] = self._index_evidence(evidence, facts)
        self._index_tags(seg_tags)

        # Rule I3: mark as indexed in PG
        execute(
            "UPDATE documents SET status='indexed' WHERE source_doc_id=%s", (source_doc_id,)
        )
        log.info("Indexed doc %s → %s", source_doc_id, stats)
        return stats

    # ── Neo4j writers ─────────────────────────────────────────────

    def _index_document(self, doc: dict) -> None:
        run_write(
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
            run_write(
                """
                MERGE (s:KnowledgeSegment {segment_id: $segment_id})
                SET s.source_doc_id  = $source_doc_id,
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
                segment_id=str(seg["segment_id"]),
                source_doc_id=str(seg["source_doc_id"]),
                segment_type=seg.get("segment_type") or "unknown",
                section_title=seg.get("section_title") or "",
                token_count=seg.get("token_count") or 0,
                confidence=float(seg.get("confidence") or 0.5),
                ontology_version=settings.ONTOLOGY_VERSION,
            )
            count += 1
        return count

    def _index_facts(self, facts: list[dict]) -> int:
        count = 0
        for f in facts:
            run_write(
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
            run_write(
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
            run_write(
                """
                MATCH (s:KnowledgeSegment {segment_id: $segment_id})
                MATCH (n:OntologyNode {node_id: $node_id})
                MERGE (s)-[r:TAGGED_WITH]->(n)
                SET r.confidence = $confidence,
                    r.tagger     = $tagger
                """,
                segment_id=str(tag["segment_id"]),
                node_id=tag["ontology_node_id"],
                confidence=float(tag.get("confidence") or 0.8),
                tagger=tag.get("tagger") or "rule",
            )