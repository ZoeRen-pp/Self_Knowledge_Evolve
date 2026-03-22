"""dependency_closure operator — full dependency tree of a node."""

from __future__ import annotations

from collections import deque

from src.db.neo4j_client import run_query

DEFAULT_RELATION_TYPES = ["DEPENDS_ON", "REQUIRES"]


def dependency_closure(
    node_id: str,
    relation_types: list[str] | None = None,
    max_depth: int = 6,
    include_optional: bool = False,
) -> dict:
    rel_types = relation_types or DEFAULT_RELATION_TYPES
    max_depth = min(max_depth, 10)
    rel_filter = "|".join(rel_types)

    # Iterative BFS (single-hop per step — works without APOC)
    visited: dict[str, dict] = {}   # node_id → {depends_on: [], requires: []}
    queue: deque[tuple[str, int]] = deque([(node_id, 0)])

    while queue:
        current_id, depth = queue.popleft()
        if depth >= max_depth or current_id in visited:
            continue
        visited[current_id] = {rt.lower(): [] for rt in rel_types}

        for rel in rel_types:
            cypher = f"""
            MATCH (a:OntologyNode {{node_id: $nid}})-[:{rel}]->(b:OntologyNode)
            RETURN b.node_id AS child_id
            """
            rows = run_query(cypher, nid=current_id)
            for row in rows:
                child_id = row["child_id"]
                visited[current_id][rel.lower()].append(child_id)
                if child_id not in visited:
                    queue.append((child_id, depth + 1))

    return {
        "root":        node_id,
        "closure":     visited,
        "total_nodes": len(visited),
        "max_depth":   max_depth,
        "relation_types": rel_types,
    }