"""ontology_inspect operator — ontology engineering health checks."""

from __future__ import annotations

import logging

from semcore.providers.base import GraphStore, RelationalStore

log = logging.getLogger(__name__)


def ontology_inspect(
    inspect_type: str,
    limit: int = 50,
    *,
    graph: GraphStore,
    store: RelationalStore | None = None,
) -> dict:
    log.debug("ontology_inspect type=%s", inspect_type)

    handler = _INSPECT_HANDLERS.get(inspect_type)
    if handler is None:
        return {"error": f"Unknown inspect_type '{inspect_type}'", "valid_types": list(_INSPECT_HANDLERS)}
    return handler(graph=graph, store=store, limit=limit)


def _inheritance_stats(*, graph: GraphStore, **_kw) -> dict:
    """Max depth, average branch factor, single-child ratio."""
    # Max inheritance depth via variable-length path
    depth_rows = graph.read(
        """
        MATCH path = (leaf)-[:SUBCLASS_OF*]->(root)
        WHERE NOT EXISTS { (root)-[:SUBCLASS_OF]->() }
        RETURN max(length(path)) AS max_depth
        """
    )
    max_depth = depth_rows[0]["max_depth"] if depth_rows else 0

    # Branch factor: for each parent, count children
    branch_rows = graph.read(
        """
        MATCH (child)-[:SUBCLASS_OF]->(parent)
        WITH parent, count(child) AS children
        RETURN avg(children) AS avg_branch,
               max(children) AS max_branch,
               count(parent) AS parent_count,
               sum(CASE WHEN children = 1 THEN 1 ELSE 0 END) AS single_child_count
        """
    )
    stats = dict(branch_rows[0]) if branch_rows else {}
    parent_count = stats.get("parent_count", 1) or 1
    single_child_ratio = (stats.get("single_child_count", 0) or 0) / parent_count

    result = {
        "inspect_type": "inheritance_stats",
        "max_depth": max_depth,
        "avg_branch_factor": round(float(stats.get("avg_branch", 0) or 0), 2),
        "max_branch_factor": stats.get("max_branch", 0),
        "parent_count": stats.get("parent_count", 0),
        "single_child_count": stats.get("single_child_count", 0),
        "single_child_ratio": round(single_child_ratio, 4),
    }
    log.info("inheritance_stats: depth=%s branch=%.1f single_child=%.0f%%",
             max_depth, result["avg_branch_factor"], single_child_ratio * 100)
    return result


def _single_child(*, graph: GraphStore, limit: int, **_kw) -> dict:
    """Parent nodes with exactly one child — possibly too fine-grained."""
    rows = graph.read(
        """
        MATCH (child)-[:SUBCLASS_OF]->(parent)
        WITH parent, collect(child.node_id) AS children
        WHERE size(children) = 1
        RETURN parent.node_id AS node_id, parent.canonical_name AS name,
               children[0] AS only_child
        ORDER BY parent.node_id
        LIMIT $limit
        """,
        limit=limit,
    )
    nodes = [dict(r) for r in rows]
    log.info("single_child: %d nodes", len(nodes))
    return {"inspect_type": "single_child", "count": len(nodes), "nodes": nodes}


def _no_alias(*, graph: GraphStore, limit: int, **_kw) -> dict:
    """Ontology nodes with no aliases — harder to discover via search."""
    rows = graph.read(
        """
        MATCH (n:OntologyNode)
        WHERE n.lifecycle_state = 'active'
          AND NOT EXISTS { MATCH (:Alias)-[:ALIAS_OF]->(n) }
        RETURN n.node_id AS node_id, n.canonical_name AS name
        ORDER BY n.node_id
        LIMIT $limit
        """,
        limit=limit,
    )
    nodes = [dict(r) for r in rows]
    log.info("no_alias: %d nodes without aliases", len(nodes))
    return {"inspect_type": "no_alias", "count": len(nodes), "nodes": nodes}


def _alias_conflicts(*, graph: GraphStore, limit: int, **_kw) -> dict:
    """Surface forms that map to multiple ontology nodes — ambiguity."""
    rows = graph.read(
        """
        MATCH (a:Alias)-[:ALIAS_OF]->(n:OntologyNode)
        WITH toLower(a.surface_form) AS form, collect(DISTINCT n.node_id) AS targets
        WHERE size(targets) > 1
        RETURN form AS surface_form, targets
        ORDER BY size(targets) DESC
        LIMIT $limit
        """,
        limit=limit,
    )
    conflicts = [dict(r) for r in rows]
    log.info("alias_conflicts: %d ambiguous surface forms", len(conflicts))
    return {"inspect_type": "alias_conflicts", "count": len(conflicts), "conflicts": conflicts}


def _relation_candidates(*, store: RelationalStore | None, limit: int, **_kw) -> dict:
    """Candidate relation types discovered by LLM but not in ontology."""
    if store is None:
        return {"inspect_type": "relation_candidates", "count": 0, "candidates": []}
    rows = store.fetchall(
        """SELECT normalized_form, surface_forms, source_count, review_status,
                  examples, first_seen_at, last_seen_at
           FROM governance.evolution_candidates
           WHERE candidate_type = 'relation'
           ORDER BY source_count DESC
           LIMIT %s""",
        (limit,),
    )
    candidates = [dict(r) for r in rows]
    log.info("relation_candidates: %d found", len(candidates))
    return {"inspect_type": "relation_candidates", "count": len(candidates), "candidates": candidates}


_INSPECT_HANDLERS = {
    "inheritance_stats": _inheritance_stats,
    "single_child": _single_child,
    "no_alias": _no_alias,
    "alias_conflicts": _alias_conflicts,
    "relation_candidates": _relation_candidates,
}