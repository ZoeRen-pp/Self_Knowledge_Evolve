"""Stage 5: Deduplication + merge — rules D1-D5."""

from __future__ import annotations

import logging
import uuid

from semcore.core.context import PipelineContext
from semcore.pipeline.base import Stage
from semcore.providers.base import RelationalStore

from src.utils.hashing import hamming_distance, jaccard_similarity
from src.utils.text import normalize_text

log = logging.getLogger(__name__)

SIMHASH_NEAR_DUP_THRESHOLD = 3    # hamming distance ≤ 3
JACCARD_DUP_THRESHOLD      = 0.85


class DedupStage(Stage):
    name = "dedup"

    def __init__(self) -> None:
        self._store: RelationalStore | None = None

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._store = app.store
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        seg_stats  = self.process_document(source_doc_id)
        fact_stats = self.process_facts(source_doc_id)
        self.set_output(ctx, {**seg_stats, **fact_stats})
        return ctx

    def process_document(self, source_doc_id: str) -> dict:
        """Rule D2: segment-level SimHash dedup within a document."""
        store = self._store
        segments = store.fetchall(
            "SELECT segment_id, raw_text, simhash_value, source_doc_id FROM segments "
            "WHERE source_doc_id=%s AND lifecycle_state='active' ORDER BY segment_index",
            (source_doc_id,),
        )
        log.debug("Dedup segments: doc=%s active_segments=%d", source_doc_id, len(segments))
        superseded = 0
        n = len(segments)

        for i in range(n):
            for j in range(i + 1, n):
                a, b = segments[i], segments[j]
                if a.get("simhash_value") is None or b.get("simhash_value") is None:
                    continue
                hd = hamming_distance(a["simhash_value"], b["simhash_value"])
                if hd <= SIMHASH_NEAR_DUP_THRESHOLD:
                    # Confirm with Jaccard
                    norm_a = self._normalize_for_dedup(a["raw_text"])
                    norm_b = self._normalize_for_dedup(b["raw_text"])
                    if jaccard_similarity(norm_a, norm_b) >= JACCARD_DUP_THRESHOLD:
                        # Supersede segment j (later one)
                        store.execute(
                            "UPDATE segments SET lifecycle_state='superseded' WHERE segment_id=%s",
                            (b["segment_id"],),
                        )
                        log.debug("  superseded seg=%s (hamming=%d)", str(b["segment_id"])[:12], hd)
                        superseded += 1

        log.info("Dedup doc=%s: %d/%d segments superseded", source_doc_id, superseded, n)
        return {"segments_superseded": superseded}

    def process_facts(self, source_doc_id: str) -> dict:
        """Rule D3-D5: fact-level dedup and conflict detection."""
        store = self._store
        # Get all facts from this document via evidence → segment → doc join
        new_facts = store.fetchall(
            """
            SELECT DISTINCT f.*
            FROM facts f
            JOIN evidence e ON f.fact_id = e.fact_id
            WHERE e.source_doc_id = %s AND f.lifecycle_state = 'active'
            """,
            (source_doc_id,),
        )

        log.debug("Dedup facts: doc=%s active_facts=%d", source_doc_id, len(new_facts))
        merged = 0
        conflicted = 0

        for fact in new_facts:
            # Skip facts already superseded by an earlier D3 merge in this loop
            current = store.fetchone(
                "SELECT lifecycle_state FROM facts WHERE fact_id=%s", (fact["fact_id"],)
            )
            if not current or current.get("lifecycle_state") != "active":
                continue

            # Rule D3 condition A: exact (subject, predicate, object) match
            existing = store.fetchall(
                """
                SELECT fact_id, confidence, merge_cluster_id
                FROM facts
                WHERE subject=%s AND predicate=%s AND object=%s
                  AND fact_id != %s AND lifecycle_state='active'
                """,
                (fact["subject"], fact["predicate"], fact["object"], fact["fact_id"]),
            )

            if existing:
                # Multi-source merge: keep one canonical fact (highest confidence),
                # supersede the rest so D4 sees only one active fact per (S,P,O).
                all_dupes = [fact] + list(existing)
                canonical_id = max(
                    all_dupes, key=lambda f: f.get("confidence") or 0
                )["fact_id"]
                cluster_id = next(
                    (f["merge_cluster_id"] for f in all_dupes if f.get("merge_cluster_id")),
                    str(uuid.uuid4()),
                )
                for dup in all_dupes:
                    if dup["fact_id"] == canonical_id:
                        store.execute(
                            "UPDATE facts SET merge_cluster_id=%s WHERE fact_id=%s",
                            (cluster_id, dup["fact_id"]),
                        )
                    else:
                        store.execute(
                            "UPDATE facts SET merge_cluster_id=%s,"
                            " lifecycle_state='superseded'"
                            " WHERE fact_id=%s AND lifecycle_state='active'",
                            (cluster_id, dup["fact_id"]),
                        )
                merged += 1
                continue

            # Rule D3b: semantic dedup — same subject+object, different predicate text
            # but embedding similarity > 0.90 on source segments → merge
            sem_merged = self._semantic_fact_dedup(fact, source_doc_id)
            if sem_merged:
                merged += sem_merged
                continue

            # Rule D4: conflict detection — same subject+predicate, different object
            conflicts = store.fetchall(
                """
                SELECT fact_id FROM facts
                WHERE subject=%s AND predicate=%s AND object != %s
                  AND lifecycle_state='active'
                """,
                (fact["subject"], fact["predicate"], fact["object"]),
            )
            for conf_fact in conflicts:
                store.execute(
                    "UPDATE facts SET lifecycle_state='conflicted' WHERE fact_id IN (%s,%s)",
                    (fact["fact_id"], conf_fact["fact_id"]),  # Note: use separate params
                )
                store.execute(
                    """
                    INSERT INTO governance.conflict_records (conflict_id, fact_id_a, fact_id_b, conflict_type)
                    VALUES (%s,%s,%s,'contradictory_value')
                    ON CONFLICT DO NOTHING
                    """,
                    (str(uuid.uuid4()), fact["fact_id"], conf_fact["fact_id"]),
                )
                conflicted += 1

        log.info("Doc %s: %d facts merged, %d conflicts", source_doc_id, merged, conflicted)
        return {"facts_merged": merged, "facts_conflicted": conflicted}

    def _semantic_fact_dedup(self, fact: dict, source_doc_id: str) -> int:
        """Rule D3b: embedding-based semantic fact dedup.

        Finds facts with same (subject, object) pair where the source segment
        texts are semantically equivalent (cosine > 0.90). Merges them.
        Returns number of merges performed.
        """
        from src.config.settings import settings
        if not getattr(settings, "EMBEDDING_ENABLED", False):
            return 0

        store = self._store
        # Find facts with same subject+object but potentially different predicates
        similar_facts = store.fetchall(
            """SELECT f.fact_id, f.predicate, f.confidence, e.segment_id
               FROM facts f
               JOIN evidence e ON f.fact_id = e.fact_id
               WHERE f.subject = %s AND f.object = %s
                 AND f.fact_id != %s AND f.lifecycle_state = 'active'""",
            (fact["subject"], fact["object"], fact["fact_id"]),
        )
        if not similar_facts:
            return 0

        # Get source segment text for the current fact
        my_evidence = store.fetchone(
            "SELECT segment_id FROM evidence WHERE fact_id = %s LIMIT 1",
            (fact["fact_id"],),
        )
        if not my_evidence or not my_evidence.get("segment_id"):
            return 0

        my_seg = store.fetchone(
            "SELECT raw_text FROM segments WHERE segment_id = %s",
            (my_evidence["segment_id"],),
        )
        if not my_seg or not my_seg.get("raw_text"):
            return 0

        try:
            from src.utils.embedding import get_embeddings
            import numpy as np

            texts = [my_seg["raw_text"][:512]]
            fact_map = []
            for sf in similar_facts:
                if not sf.get("segment_id"):
                    continue
                seg = store.fetchone(
                    "SELECT raw_text FROM segments WHERE segment_id = %s",
                    (sf["segment_id"],),
                )
                if seg and seg.get("raw_text"):
                    texts.append(seg["raw_text"][:512])
                    fact_map.append(sf)

            if len(texts) < 2:
                return 0

            vecs = get_embeddings(texts)
            if vecs is None:
                return 0

            emb = np.array(vecs)
            my_vec = emb[0]
            merged = 0

            for i, sf in enumerate(fact_map):
                cos = float(np.dot(my_vec, emb[i + 1]))
                if cos >= 0.90:
                    # Semantic duplicate — supersede the lower-confidence one
                    if (sf.get("confidence") or 0) >= (fact.get("confidence") or 0):
                        loser_id = fact["fact_id"]
                    else:
                        loser_id = sf["fact_id"]
                    store.execute(
                        "UPDATE facts SET lifecycle_state='superseded' WHERE fact_id=%s AND lifecycle_state='active'",
                        (loser_id,),
                    )
                    log.debug("  Semantic dedup: merged fact %s (cosine=%.3f)", loser_id[:12], cos)
                    merged += 1

            return merged
        except Exception as exc:
            log.debug("Semantic fact dedup failed: %s", exc)
            return 0

    def _normalize_for_dedup(self, text: str) -> str:
        """Rule D5: normalize before dedup — lowercase, collapse whitespace."""
        return normalize_text(text)