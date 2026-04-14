"""context_assemble operator — assemble full agent context package.

Given node IDs or keywords, retrieves:
1. Five-layer reasoning chain (concept → mechanism → method → condition → scenario)
2. All related segments with FULL text (not truncated)
3. RST-linked segment chains for coherent context
4. Evidence provenance (source, rank, confidence)
5. Related facts with descriptions

Returns a structured context package ready for LLM consumption.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from semcore.providers.base import GraphStore, RelationalStore

log = logging.getLogger(__name__)


def context_assemble(
    node_ids: list[str] | None = None,
    keywords: list[str] | None = None,
    max_segments: int = 50,
    max_hops: int = 2,
    *,
    store: RelationalStore,
    graph: GraphStore,
) -> dict:
    """Assemble a complete context package for agent reasoning.

    Args:
        node_ids: Ontology node IDs to build context around.
        keywords: If no node_ids, search segments by keywords.
        max_segments: Maximum segments to return.
        max_hops: How far to traverse from seed nodes for related concepts.

    Returns:
        Structured context with reasoning chain, full segment texts,
        RST chains, facts, and provenance.
    """
    log.info("context_assemble: nodes=%s keywords=%s", node_ids, keywords)

    # Resolve seed nodes
    seed_nodes = node_ids or []
    if not seed_nodes and keywords:
        seed_nodes = _resolve_keywords(keywords, store)

    if not seed_nodes:
        return {"error": "No node_ids or keywords provided", "context": {}}

    # 1. Node descriptions
    node_descriptions = _get_node_descriptions(seed_nodes, graph)

    # 2. Five-layer reasoning chain
    reasoning_chain = _build_reasoning_chain(seed_nodes, graph)

    # 3. Related facts with descriptions
    facts = _get_related_facts(seed_nodes, max_hops, graph)

    # 4. All related segments (FULL text)
    segments, segment_ids = _get_related_segments(seed_nodes, max_segments, store)

    # 5. RST chains — link segments into coherent reading order
    rst_chains = _get_rst_chains(segment_ids, store)

    # 6. Evidence provenance
    evidence = _get_evidence_provenance(seed_nodes, store)

    context = {
        "seed_nodes": seed_nodes,
        "node_descriptions": node_descriptions,
        "reasoning_chain": reasoning_chain,
        "facts": facts,
        "segments": segments,
        "rst_chains": rst_chains,
        "evidence": evidence,
        "stats": {
            "nodes_described": len(node_descriptions),
            "facts_count": len(facts),
            "segments_count": len(segments),
            "total_tokens": sum(s.get("token_count", 0) for s in segments),
            "rst_links": len(rst_chains),
        },
    }

    log.info("context_assemble: %d nodes, %d facts, %d segments (%d tokens), %d RST links",
             len(node_descriptions), len(facts), len(segments),
             context["stats"]["total_tokens"], len(rst_chains))
    return context


def _resolve_keywords(keywords: list[str], store: RelationalStore) -> list[str]:
    """Resolve keywords to ontology node IDs via alias lookup."""
    node_ids = []
    for kw in keywords:
        rows = store.fetchall(
            "SELECT DISTINCT canonical_node_id FROM lexicon_aliases WHERE lower(surface_form) = %s",
            (kw.lower(),),
        )
        for r in rows:
            if r["canonical_node_id"] not in node_ids:
                node_ids.append(r["canonical_node_id"])
    return node_ids


def _get_node_descriptions(node_ids: list[str], graph: GraphStore) -> list[dict]:
    """Get full descriptions for all nodes."""
    results = []
    for nid in node_ids:
        rows = graph.read(
            """MATCH (n {node_id: $nid})
               RETURN n.node_id AS node_id, n.canonical_name AS name,
                      n.description AS description, labels(n) AS labels""",
            nid=nid,
        )
        if rows:
            r = rows[0]
            results.append({
                "node_id": r["node_id"],
                "name": r["name"],
                "description": r.get("description") or "",
                "labels": r.get("labels") or [],
            })
    return results


def _build_reasoning_chain(seed_nodes: list[str], graph: GraphStore) -> list[dict]:
    """Build five-layer reasoning chains from seed nodes.

    Traverses: concept ←explains→ mechanism ←implemented_by→ method
               ←applies_to→ condition ←composed_of→ scenario
    """
    chains = []

    for nid in seed_nodes:
        chain = {"seed": nid, "layers": {}}

        # concept → mechanism (explains)
        mechs = graph.read(
            """MATCH (c {node_id: $nid})-[:EXPLAINS]->(m)
               RETURN m.node_id AS id, m.canonical_name AS name, m.description AS desc""",
            nid=nid,
        )
        if mechs:
            chain["layers"]["mechanism"] = [
                {"node_id": m["id"], "name": m["name"], "description": m.get("desc") or "",
                 "relation": "explains"}
                for m in mechs
            ]

        # mechanism → method (implemented_by)
        for mech in (mechs or []):
            methods = graph.read(
                """MATCH (m {node_id: $mid})-[:IMPLEMENTED_BY]->(mt)
                   RETURN mt.node_id AS id, mt.canonical_name AS name, mt.description AS desc""",
                mid=mech["id"],
            )
            if methods:
                chain["layers"].setdefault("method", []).extend([
                    {"node_id": mt["id"], "name": mt["name"], "description": mt.get("desc") or "",
                     "relation": "implemented_by", "from_mechanism": mech["id"]}
                    for mt in methods
                ])

        # method → condition (applies_to)
        for mt in chain["layers"].get("method", []):
            conds = graph.read(
                """MATCH (mt {node_id: $mtid})-[:APPLIES_TO]->(c)
                   RETURN c.node_id AS id, c.canonical_name AS name, c.description AS desc""",
                mtid=mt["node_id"],
            )
            if conds:
                chain["layers"].setdefault("condition", []).extend([
                    {"node_id": c["id"], "name": c["name"], "description": c.get("desc") or "",
                     "relation": "applies_to", "from_method": mt["node_id"]}
                    for c in conds
                ])

        # method/condition → scenario (composed_of, reverse)
        method_ids = [m["node_id"] for m in chain["layers"].get("method", [])]
        if method_ids:
            for mtid in method_ids:
                scenes = graph.read(
                    """MATCH (s)-[:COMPOSED_OF]->(mt {node_id: $mtid})
                       RETURN s.node_id AS id, s.canonical_name AS name, s.description AS desc""",
                    mtid=mtid,
                )
                if scenes:
                    chain["layers"].setdefault("scenario", []).extend([
                        {"node_id": s["id"], "name": s["name"], "description": s.get("desc") or "",
                         "relation": "composed_of", "includes_method": mtid}
                        for s in scenes
                    ])

        # Deduplicate within each layer
        for layer in chain["layers"]:
            seen = set()
            unique = []
            for item in chain["layers"][layer]:
                if item["node_id"] not in seen:
                    seen.add(item["node_id"])
                    unique.append(item)
            chain["layers"][layer] = unique

        if chain["layers"]:
            chains.append(chain)

    return chains


def _get_related_facts(seed_nodes: list[str], max_hops: int, graph: GraphStore) -> list[dict]:
    """Get all facts involving seed nodes (as subject or object)."""
    facts = []
    seen = set()

    for nid in seed_nodes:
        rows = graph.read(
            """MATCH (a)-[r]->(b)
               WHERE (a.node_id = $nid OR b.node_id = $nid)
                 AND r.predicate IS NOT NULL
               RETURN a.node_id AS subject, a.canonical_name AS subject_name,
                      r.predicate AS predicate, type(r) AS rel_type,
                      b.node_id AS object, b.canonical_name AS object_name,
                      r.confidence AS confidence
               ORDER BY r.confidence DESC
               LIMIT 30""",
            nid=nid,
        )
        for r in rows:
            key = (r["subject"], r["predicate"], r["object"])
            if key not in seen:
                seen.add(key)
                facts.append({
                    "subject": r["subject"],
                    "subject_name": r.get("subject_name") or r["subject"],
                    "predicate": r["predicate"],
                    "object": r["object"],
                    "object_name": r.get("object_name") or r["object"],
                    "confidence": r.get("confidence"),
                })

    return facts


def _get_related_segments(
    seed_nodes: list[str], max_segments: int, store: RelationalStore,
) -> tuple[list[dict], list[str]]:
    """Get ALL related segments with FULL text (not truncated)."""
    if not seed_nodes:
        return [], []

    # Find segments tagged with any of the seed nodes
    placeholders = ",".join(["%s"] * len(seed_nodes))
    rows = store.fetchall(
        f"""SELECT DISTINCT s.segment_id, s.source_doc_id, s.segment_type,
                   s.section_title, s.raw_text, s.token_count, s.confidence,
                   s.content_source, d.title AS doc_title, d.source_url, d.source_rank,
                   array_agg(DISTINCT st.ontology_node_id) AS matched_nodes
            FROM segments s
            JOIN segment_tags st ON s.segment_id = st.segment_id
            JOIN documents d ON s.source_doc_id = d.source_doc_id
            WHERE st.tag_type = 'canonical'
              AND st.ontology_node_id IN ({placeholders})
              AND s.lifecycle_state = 'active'
            GROUP BY s.segment_id, s.source_doc_id, s.segment_type,
                     s.section_title, s.raw_text, s.token_count, s.confidence,
                     s.content_source, d.title, d.source_url, d.source_rank
            ORDER BY s.confidence DESC, s.token_count DESC
            LIMIT %s""",
        (*seed_nodes, max_segments),
    )

    segments = []
    segment_ids = []
    for r in rows:
        segments.append({
            "segment_id": str(r["segment_id"]),
            "segment_type": r["segment_type"],
            "section_title": r.get("section_title") or "",
            "text": r["raw_text"],  # FULL text, not truncated
            "token_count": r.get("token_count") or 0,
            "confidence": float(r.get("confidence") or 0),
            "matched_nodes": r.get("matched_nodes") or [],
            "source": {
                "doc_title": r.get("doc_title") or "",
                "url": r.get("source_url") or "",
                "rank": r.get("source_rank") or "C",
            },
        })
        segment_ids.append(str(r["segment_id"]))

    return segments, segment_ids


def _get_rst_chains(segment_ids: list[str], store: RelationalStore) -> list[dict]:
    """Get RST relations between the returned segments, for coherent ordering."""
    if len(segment_ids) < 2:
        return []

    placeholders = ",".join(["%s"] * len(segment_ids))
    rows = store.fetchall(
        f"""SELECT src_edu_id, dst_edu_id, relation_type, nuclearity, relation_source
            FROM t_rst_relation
            WHERE src_edu_id::text IN ({placeholders})
              AND dst_edu_id::text IN ({placeholders})
            ORDER BY src_edu_id""",
        (*segment_ids, *segment_ids),
    )
    return [
        {
            "from_segment": str(r["src_edu_id"]),
            "to_segment":   str(r["dst_edu_id"]),
            "relation":     r["relation_type"],
            "nuclearity":   r.get("nuclearity") or "NN",
            "source":       r.get("relation_source") or "rule",
        }
        for r in rows
    ]


def _get_evidence_provenance(seed_nodes: list[str], store: RelationalStore) -> list[dict]:
    """Get evidence records linking facts to source documents."""
    if not seed_nodes:
        return []

    placeholders = ",".join(["%s"] * len(seed_nodes))
    rows = store.fetchall(
        f"""SELECT DISTINCT f.subject, f.predicate, f.object, f.confidence,
                   e.source_rank, e.extraction_method,
                   d.title AS doc_title, d.source_url
            FROM facts f
            JOIN evidence e ON f.fact_id = e.fact_id
            JOIN documents d ON e.source_doc_id = d.source_doc_id
            WHERE f.lifecycle_state = 'active'
              AND (f.subject IN ({placeholders}) OR f.object IN ({placeholders}))
            ORDER BY e.source_rank, f.confidence DESC
            LIMIT 30""",
        (*seed_nodes, *seed_nodes),
    )
    return [
        {
            "fact": f"{r['subject']} {r['predicate']} {r['object']}",
            "confidence": float(r.get("confidence") or 0),
            "source_rank": r.get("source_rank") or "C",
            "extraction_method": r.get("extraction_method") or "",
            "doc_title": r.get("doc_title") or "",
            "url": r.get("source_url") or "",
        }
        for r in rows
    ]