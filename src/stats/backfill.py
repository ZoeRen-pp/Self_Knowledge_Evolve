"""BackfillWorker — incremental backfill after concept approval.

Runs in a background daemon thread. Searches existing segments for
mentions of the new term, adds tags, extracts facts, indexes to Neo4j.
Never blocks the main Pipeline or API.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)


class BackfillWorker:
    """Run incremental backfill in a background thread."""

    def __init__(self, app):
        self._app = app

    def backfill_concept(self, node_id: str, surface_forms: list[str]) -> None:
        """Start background thread to backfill a newly accepted concept."""
        t = threading.Thread(
            target=self._run_concept_backfill,
            args=(node_id, surface_forms),
            name=f"backfill-{node_id}",
            daemon=True,
        )
        t.start()
        log.info("Backfill thread started for %s (terms=%s)", node_id, surface_forms)

    def _run_concept_backfill(self, node_id: str, surface_forms: list[str]) -> None:
        t0 = time.monotonic()
        store = self._app.store
        graph = self._app.graph
        llm = self._app.llm
        ontology = self._app.ontology

        try:
            # Step 1: Search segments containing any of the surface forms
            matching_segments = self._find_matching_segments(store, surface_forms)
            log.info("Backfill %s: found %d matching segments", node_id, len(matching_segments))

            if not matching_segments:
                return

            tags_added = 0
            facts_added = 0

            for seg in matching_segments:
                seg_id = str(seg["segment_id"])

                # Step 2: Add canonical tag
                tag_added = self._add_tag(store, seg_id, node_id)
                if tag_added:
                    tags_added += 1

                # Step 3: Extract new facts (LLM preferred)
                new_facts = self._extract_facts_for_segment(
                    seg, node_id, store, graph, llm, ontology,
                )
                facts_added += len(new_facts)

            # Step 4: Index new tags to Neo4j
            if tags_added > 0:
                self._index_new_tags(graph, node_id, matching_segments)

            elapsed = time.monotonic() - t0
            log.info(
                "Backfill %s complete: segments=%d tags=%d facts=%d elapsed=%.1fs",
                node_id, len(matching_segments), tags_added, facts_added, elapsed,
            )

        except Exception as exc:
            log.error("Backfill %s failed: %s", node_id, exc, exc_info=True)

    def _find_matching_segments(
        self, store, surface_forms: list[str],
    ) -> list[dict]:
        """Search segments containing any of the given terms (case-insensitive)."""
        all_segments = []
        seen_ids = set()

        for term in surface_forms:
            rows = store.fetchall(
                """SELECT segment_id, source_doc_id, raw_text, segment_type
                   FROM segments
                   WHERE lifecycle_state = 'active'
                     AND raw_text ILIKE %s
                   LIMIT 500""",
                (f"%{term}%",),
            )
            for r in rows:
                sid = str(r["segment_id"])
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    all_segments.append(dict(r))

        return all_segments

    def _add_tag(self, store, segment_id: str, node_id: str) -> bool:
        """Add a canonical tag if it doesn't exist."""
        existing = store.fetchone(
            """SELECT 1 FROM segment_tags
               WHERE segment_id = %s AND ontology_node_id = %s AND tag_type = 'canonical'""",
            (segment_id, node_id),
        )
        if existing:
            return False

        store.execute(
            """INSERT INTO segment_tags
                   (segment_id, tag_type, tag_value, ontology_node_id, confidence, tagger, ontology_version)
               VALUES (%s, 'canonical', %s, %s, 0.85, 'backfill', 'evolved')
               ON CONFLICT DO NOTHING""",
            (segment_id, node_id.split(".")[-1], node_id),
        )
        return True

    def _extract_facts_for_segment(
        self, seg: dict, node_id: str,
        store, graph, llm, ontology,
    ) -> list[dict]:
        """Extract facts involving the new node from a segment."""
        if not llm.is_enabled():
            return []

        text = seg.get("raw_text", "")
        if not text.strip():
            return []

        # Get existing canonical nodes for this segment
        existing_tags = store.fetchall(
            "SELECT ontology_node_id FROM segment_tags WHERE segment_id = %s AND tag_type = 'canonical'",
            (seg["segment_id"],),
        )
        candidate_ids = [t["ontology_node_id"] for t in existing_tags if t.get("ontology_node_id")]
        if node_id not in candidate_ids:
            candidate_ids.append(node_id)

        valid_relations = list(ontology.relation_ids)

        raw_triples = llm.extract_triples(text, candidate_ids, valid_relations)
        facts = []
        for triple in raw_triples:
            subj = triple.get("subject", "")
            pred = triple.get("predicate", "")
            obj = triple.get("object", "")
            if not subj or not pred or not obj or subj == obj:
                continue
            if not ontology.is_valid_relation(pred):
                continue
            # Only keep facts involving the new node
            if node_id not in (subj, obj):
                continue

            fact_id = str(uuid.uuid4())
            from src.utils.confidence import score_fact
            conf = score_fact(
                source_rank=seg.get("source_rank", "B"),
                extraction_method="llm",
                ontology_fit=0.75,
                cross_source_consistency=0.5,
                temporal_validity=1.0,
            )

            # Write to PG
            store.execute(
                """INSERT INTO facts (fact_id, subject, predicate, object, confidence, lifecycle_state, ontology_version)
                   VALUES (%s, %s, %s, %s, %s, 'active', 'evolved')
                   ON CONFLICT DO NOTHING""",
                (fact_id, subj, pred, obj, conf),
            )
            store.execute(
                """INSERT INTO evidence (evidence_id, fact_id, source_doc_id, segment_id,
                       source_rank, extraction_method, evidence_score)
                   VALUES (%s, %s, %s, %s, 'B', 'llm', %s)
                   ON CONFLICT DO NOTHING""",
                (str(uuid.uuid4()), fact_id, seg.get("source_doc_id"), seg["segment_id"], conf),
            )

            # Write to Neo4j
            import re
            rel_type = re.sub(r"[^a-zA-Z0-9_]", "_", pred).upper()
            graph.write(
                f"""
                MERGE (f:Fact {{fact_id: $fact_id}})
                SET f.subject = $subj, f.predicate = $pred, f.object = $obj,
                    f.confidence = $conf, f.lifecycle_state = 'active'
                WITH f
                MATCH (a:OntologyNode {{node_id: $subj}})
                MATCH (b:OntologyNode {{node_id: $obj}})
                MERGE (a)-[r:{rel_type} {{fact_id: $fact_id}}]->(b)
                SET r.confidence = $conf, r.predicate = $pred
                """,
                fact_id=fact_id, subj=subj, pred=pred, obj=obj, conf=conf,
            )
            facts.append({"fact_id": fact_id, "subject": subj, "predicate": pred, "object": obj})

        return facts

    def _index_new_tags(self, graph, node_id: str, segments: list[dict]) -> None:
        """Write TAGGED_WITH edges to Neo4j for newly tagged segments."""
        for seg in segments:
            graph.write(
                """
                MATCH (s:KnowledgeSegment {segment_id: $seg_id})
                MATCH (n:OntologyNode {node_id: $node_id})
                MERGE (s)-[r:TAGGED_WITH]->(n)
                SET r.confidence = 0.85, r.tagger = 'backfill', r.tag_type = 'canonical'
                """,
                seg_id=str(seg["segment_id"]), node_id=node_id,
            )