"""semantic_path operator — shortest path between two ontology nodes."""

from __future__ import annotations

from src.db.neo4j_client import run_query

_RELATION_POLICIES = {
    "dependency": ["DEPENDS_ON", "REQUIRES"],
    "causal":     ["CAUSES", "IMPACTS"],
    "all":        [],  # no filter
}


def path_infer(
    start_node_id: str,
    end_node_id: str,
    relation_policy: str = "all",
    max_hops: int = 5,
    min_confidence: float = 0.5,
) -> dict:
    max_hops = min(max(max_hops, 1), 8)
    rel_types = _RELATION_POLICIES.get(relation_policy, [])

    rel_filter = ""
    if rel_types:
        rel_filter = ":" + "|".join(rel_types)

    cypher = f"""
    MATCH (a:OntologyNode {{node_id: $start}}), (b:OntologyNode {{node_id: $end}})
    MATCH path = shortestPath((a)-[r{rel_filter}*1..{max_hops}]-(b))
    WHERE ALL(rel IN relationships(path) WHERE coalesce(rel.confidence, 1.0) >= $min_conf)
    RETURN
        [node IN nodes(path) | node.node_id]          AS node_ids,
        [node IN nodes(path) | node.canonical_name]   AS node_names,
        [rel  IN relationships(path) | type(rel)]      AS rel_types,
        [rel  IN relationships(path) | coalesce(rel.confidence, 1.0)] AS rel_confs
    LIMIT 5
    """
    rows = run_query(
        cypher, start=start_node_id, end=end_node_id, min_conf=min_confidence
    )

    paths = []
    for row in rows:
        node_ids  = row["node_ids"]
        node_names = row["node_names"]
        rel_types_row = row["rel_types"]
        rel_confs = row["rel_confs"]

        hops = len(rel_types_row)
        path_steps = []
        for i, nid in enumerate(node_ids):
            step: dict = {"node_id": nid, "canonical_name": node_names[i]}
            if i < len(rel_types_row):
                step["relation"]   = rel_types_row[i]
                step["confidence"] = rel_confs[i]
            path_steps.append(step)

        path_confidence = min(rel_confs) if rel_confs else 1.0
        paths.append({"hops": hops, "path": path_steps, "path_confidence": path_confidence})

    return {
        "start_node_id":    start_node_id,
        "end_node_id":      end_node_id,
        "relation_policy":  relation_policy,
        "paths":            paths,
        "path_count":       len(paths),
    }