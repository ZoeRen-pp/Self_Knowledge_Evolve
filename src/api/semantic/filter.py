"""semantic_filter operator — parameterized filtering over facts/segments/concepts."""

from __future__ import annotations

from src.db.postgres import fetchall, fetchone
from src.db.neo4j_client import run_query


def filter_objects(
    object_type: str,
    filters: dict,
    sort_by: str = "confidence",
    sort_order: str = "desc",
    page: int = 1,
    page_size: int = 20,
) -> dict:
    page_size = min(page_size, 100)
    offset = (page - 1) * page_size

    if object_type == "fact":
        return _filter_facts(filters, sort_by, sort_order, page_size, offset, page)
    elif object_type == "segment":
        return _filter_segments(filters, sort_by, sort_order, page_size, offset, page)
    elif object_type == "concept":
        return _filter_concepts(filters, page_size, offset, page)
    else:
        return {"error": f"Unknown object_type '{object_type}'"}


def _filter_facts(filters: dict, sort_by: str, sort_order: str, limit: int, offset: int, page: int) -> dict:
    where: list[str] = ["f.lifecycle_state = 'active'"]
    params: list = []

    _allowed_sort = {"confidence", "created_at", "updated_at"}
    safe_sort = sort_by if sort_by in _allowed_sort else "confidence"
    safe_order = "DESC" if sort_order.lower() == "desc" else "ASC"

    if filters.get("min_confidence") is not None:
        where.append("f.confidence >= %s")
        params.append(filters["min_confidence"])
    if filters.get("domain"):
        where.append("f.domain = %s")
        params.append(filters["domain"])
    if filters.get("lifecycle_state"):
        where.append("f.lifecycle_state = %s")
        params.append(filters["lifecycle_state"])
    if filters.get("after_date"):
        where.append("f.created_at >= %s")
        params.append(filters["after_date"])

    where_sql = " AND ".join(where)
    count_row = fetchone(f"SELECT COUNT(*) as cnt FROM facts f WHERE {where_sql}", tuple(params))
    total = count_row["cnt"] if count_row else 0

    items = fetchall(
        f"SELECT * FROM facts f WHERE {where_sql} ORDER BY f.{safe_sort} {safe_order} LIMIT %s OFFSET %s",
        tuple(params) + (limit, offset),
    )
    return {"items": items, "total": total, "page": page, "page_size": limit}


def _filter_segments(filters: dict, sort_by: str, sort_order: str, limit: int, offset: int, page: int) -> dict:
    where: list[str] = ["s.lifecycle_state = 'active'"]
    params: list = []

    safe_sort = "confidence" if sort_by not in {"confidence", "created_at"} else sort_by
    safe_order = "DESC" if sort_order.lower() == "desc" else "ASC"

    if filters.get("min_confidence") is not None:
        where.append("s.confidence >= %s")
        params.append(filters["min_confidence"])
    if filters.get("tags"):
        tag_list = filters["tags"] if isinstance(filters["tags"], list) else [filters["tags"]]
        placeholders = ",".join(["%s"] * len(tag_list))
        where.append(
            f"EXISTS (SELECT 1 FROM segment_tags st WHERE st.segment_id=s.segment_id AND st.tag_value IN ({placeholders}))"
        )
        params.extend(tag_list)
    if filters.get("source_rank"):
        ranks = filters["source_rank"] if isinstance(filters["source_rank"], list) else [filters["source_rank"]]
        placeholders = ",".join(["%s"] * len(ranks))
        where.append(f"d.source_rank IN ({placeholders})")
        params.extend(ranks)

    where_sql = " AND ".join(where)
    count_row = fetchone(
        f"SELECT COUNT(*) as cnt FROM segments s JOIN documents d ON s.source_doc_id=d.source_doc_id WHERE {where_sql}",
        tuple(params),
    )
    total = count_row["cnt"] if count_row else 0
    items = fetchall(
        f"SELECT s.* FROM segments s JOIN documents d ON s.source_doc_id=d.source_doc_id WHERE {where_sql} ORDER BY s.{safe_sort} {safe_order} LIMIT %s OFFSET %s",
        tuple(params) + (limit, offset),
    )
    return {"items": items, "total": total, "page": page, "page_size": limit}


def _filter_concepts(filters: dict, limit: int, offset: int, page: int) -> dict:
    cypher = "MATCH (n:OntologyNode) WHERE n.lifecycle_state = 'active'"
    params: dict = {}
    if filters.get("domain"):
        cypher += " AND n.domain STARTS WITH $domain"
        params["domain"] = filters["domain"]
    cypher += " RETURN n ORDER BY n.node_id SKIP $offset LIMIT $limit"
    params.update({"offset": offset, "limit": limit})
    rows = run_query(cypher, **params)
    return {"items": [dict(r["n"]) for r in rows], "page": page, "page_size": limit}