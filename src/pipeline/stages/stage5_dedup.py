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
                        superseded += 1

        log.info("Doc %s: %d segments superseded by SimHash dedup", source_doc_id, superseded)
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

    def _normalize_for_dedup(self, text: str) -> str:
        """Rule D5: normalize before dedup — lowercase, collapse whitespace."""
        return normalize_text(text)