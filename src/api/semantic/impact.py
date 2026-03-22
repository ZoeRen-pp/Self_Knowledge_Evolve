"""impact_propagate operator — fault/change blast-radius BFS."""

from __future__ import annotations

from collections import deque

from src.db.neo4j_client import run_query

_POLICY_RELATIONS = {
    "causal":  ["CAUSES", "IMPACTS"],
    "service": ["IMPACTS", "DEPENDS_ON"],
    "all":     ["CAUSES", "IMPACTS", "DEPENDS_ON", "REQUIRES"],
}

_IMPACT_TYPE = {
    "fault":         "disrupted",
    "config_change": "affected",
    "alarm":         "triggered",
}


def impact_propagate(
    event_node_id: str,
    event_type: str = "fault",
    relation_policy: str = "causal",
    max_depth: int = 4,
    min_confidence: float = 0.6,
    context: dict | None = None,
) -> dict:
    rel_types = _POLICY_RELATIONS.get(relation_policy, _POLICY_RELATIONS["all"])
    impact_type = _IMPACT_TYPE.get(event_type, "affected")
    max_depth = min(max_depth, 6)

    # BFS through Neo4j graph
    visited: dict[str, dict] = {}  # node_id → {impact_type, confidence, via, depth}
    queue: deque[tuple[str, float, str | None, int]] = deque(
        [(event_node_id, 1.0, None, 0)]
    )

    while queue:
        current_id, acc_conf, via_rel, depth = queue.popleft()
        if depth > max_depth or current_id in visited:
            continue
        if depth > 0:  # don't add the root itself
            visited[current_id] = {
                "node_id":      current_id,
                "impact_type":  impact_type,
                "confidence":   round(acc_conf, 4),
                "via_relation": via_rel,
                "depth":        depth,
            }

        for rel in rel_types:
            cypher = f"""
            MATCH (a:OntologyNode {{node_id: $nid}})-[r:{rel}]->(b:OntologyNode)
            RETURN b.node_id AS child_id, b.canonical_name AS child_name,
                   coalesce(r.confidence, 1.0) AS edge_conf
            """
            rows = run_query(cypher, nid=current_id)
            for row in rows:
                child_id  = row["child_id"]
                edge_conf = float(row["edge_conf"])
                new_conf  = acc_conf * edge_conf
                if new_conf < min_confidence:
                    continue
                if child_id not in visited:
                    queue.append((child_id, new_conf, rel, depth + 1))

    # Enrich with canonical_name
    impact_tree = []
    for item in visited.values():
        rows = run_query(
            "MATCH (n:OntologyNode {node_id: $id}) RETURN n.canonical_name AS name LIMIT 1",
            id=item["node_id"],
        )
        item["canonical_name"] = rows[0]["name"] if rows else item["node_id"]
        impact_tree.append(item)

    impact_tree.sort(key=lambda x: (x["depth"], -x["confidence"]))

    return {
        "event":          {"node_id": event_node_id, "event_type": event_type},
        "relation_policy": relation_policy,
        "impact_tree":    impact_tree,
        "total_impacted": len(impact_tree),
    }