"""Evidence operators: evidence_rank, conflict_detect, fact_merge."""

from __future__ import annotations

import uuid

from semcore.providers.base import RelationalStore


def evidence_rank(
    fact_id: str,
    rank_by: str = "evidence_score",
    max_results: int = 10,
    *,
    store: RelationalStore,
) -> dict:
    _allowed = {"evidence_score", "source_rank", "created_at"}
    safe_col = rank_by if rank_by in _allowed else "evidence_score"

    rows = store.fetchall(
        f"""
        SELECT e.evidence_id, e.exact_span, e.source_rank, e.extraction_method,
               e.evidence_score, e.created_at,
               d.canonical_url AS source_url, d.title, d.publish_time
        FROM evidence e
        JOIN documents d ON e.source_doc_id = d.source_doc_id
        WHERE e.fact_id = %s
        ORDER BY e.{safe_col} DESC
        LIMIT %s
        """,
        (fact_id, max_results),
    )
    return {"fact_id": fact_id, "evidence": rows, "count": len(rows)}


def conflict_detect(
    topic_node_id: str,
    predicate: str | None = None,
    min_confidence: float = 0.5,
    *,
    store: RelationalStore,
) -> dict:
    extra = ""
    params: list = [topic_node_id, topic_node_id, min_confidence]
    if predicate:
        extra = " AND fa.predicate = %s"
        params.append(predicate)

    rows = store.fetchall(
        f"""
        SELECT cr.conflict_id, cr.conflict_type, cr.resolution, cr.description,
               fa.fact_id AS fact_id_a, fa.subject, fa.predicate, fa.object AS obj_a,
               fa.confidence AS conf_a, fa.lifecycle_state AS state_a,
               fb.fact_id AS fact_id_b, fb.object AS obj_b,
               fb.confidence AS conf_b
        FROM governance.conflict_records cr
        JOIN facts fa ON cr.fact_id_a = fa.fact_id
        JOIN facts fb ON cr.fact_id_b = fb.fact_id
        WHERE (fa.subject = %s OR fa.object = %s)
          AND fa.confidence >= %s{extra}
        ORDER BY cr.created_at DESC
        """,
        tuple(params),
    )
    return {"topic_node_id": topic_node_id, "conflicts": rows, "total": len(rows)}


def fact_merge(
    fact_ids: list[str],
    merge_strategy: str = "highest_confidence",
    canonical_fact: dict | None = None,
    *,
    store: RelationalStore,
) -> dict:
    if not fact_ids:
        return {"error": "fact_ids is empty"}

    facts = store.fetchall(
        f"SELECT * FROM facts WHERE fact_id IN ({','.join(['%s']*len(fact_ids))})",
        tuple(fact_ids),
    )
    if not facts:
        return {"error": "No facts found for provided IDs"}

    # Pick canonical fact
    if merge_strategy == "highest_confidence" or canonical_fact is None:
        canonical = max(facts, key=lambda f: float(f.get("confidence") or 0))
    else:
        canonical = canonical_fact  # caller provided explicit canonical

    cluster_id = str(uuid.uuid4())
    canonical_id = str(canonical["fact_id"])

    with store.transaction() as cur:
        # Mark all as merged, assign cluster
        for fid in fact_ids:
            state = "active" if str(fid) == canonical_id else "superseded"
            cur.execute(
                "UPDATE facts SET merge_cluster_id=%s, lifecycle_state=%s WHERE fact_id=%s",
                (cluster_id, state, fid),
            )
        # Re-point all evidence to canonical fact
        for fid in fact_ids:
            if str(fid) != canonical_id:
                cur.execute(
                    "UPDATE evidence SET fact_id=%s WHERE fact_id=%s",
                    (canonical_id, fid),
                )

    return {
        "merge_cluster_id": cluster_id,
        "canonical_fact_id": canonical_id,
        "merged_count":      len(fact_ids),
        "strategy":          merge_strategy,
    }