"""Candidate review — approve/reject concept and relation candidates."""

from __future__ import annotations

import json
import logging
import re
import uuid

from semcore.providers.base import GraphStore, RelationalStore

log = logging.getLogger(__name__)


def list_candidates(
    candidate_type: str = "all",
    status: str = "pending_review",
    limit: int = 20,
    *,
    store: RelationalStore,
) -> dict:
    """List candidates for review."""
    log.debug("list_candidates type=%s status=%s", candidate_type, status)

    where_parts = []
    params: list = []

    if candidate_type != "all":
        where_parts.append("candidate_type = %s")
        params.append(candidate_type)
    if status != "all":
        where_parts.append("review_status = %s")
        params.append(status)

    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    rows = store.fetchall(
        f"""SELECT candidate_id, normalized_form, surface_forms, candidate_type,
                   source_count, composite_score, review_status, examples,
                   candidate_parent_id, first_seen_at, last_seen_at
            FROM governance.evolution_candidates
            {where_clause}
            ORDER BY composite_score DESC, source_count DESC
            LIMIT %s""",
        (*params, limit),
    )
    candidates = [dict(r) for r in rows]
    log.info("list_candidates: %d results (type=%s status=%s)", len(candidates), candidate_type, status)
    return {"candidates": candidates, "count": len(candidates)}


def get_candidate(
    candidate_id: str,
    *,
    store: RelationalStore,
) -> dict:
    """Get a single candidate with full details."""
    row = store.fetchone(
        "SELECT * FROM governance.evolution_candidates WHERE candidate_id = %s",
        (candidate_id,),
    )
    if not row:
        return {"error": f"Candidate '{candidate_id}' not found"}
    return dict(row)


def approve_candidate(
    candidate_id: str,
    reviewer: str,
    note: str = "",
    parent_node_id: str | None = None,
    aliases: list[str] | None = None,
    *,
    store: RelationalStore,
    graph: GraphStore,
    ontology,
) -> dict:
    """Approve a candidate — write to ontology + trigger backfill."""
    log.info("approve_candidate id=%s reviewer=%s", candidate_id, reviewer)

    candidate = store.fetchone(
        "SELECT * FROM governance.evolution_candidates WHERE candidate_id = %s",
        (candidate_id,),
    )
    if not candidate:
        return {"error": f"Candidate '{candidate_id}' not found"}

    candidate_type = candidate.get("candidate_type", "concept")
    normalized = candidate.get("normalized_form") or ""
    surface_forms = candidate.get("surface_forms") or []

    if candidate_type == "concept":
        result = _approve_concept(
            candidate, normalized, surface_forms,
            parent_node_id=parent_node_id,
            aliases=aliases or surface_forms,
            store=store, graph=graph, ontology=ontology,
        )
    elif candidate_type == "relation":
        result = _approve_relation(
            candidate, normalized,
            store=store, graph=graph, ontology=ontology,
        )
    else:
        return {"error": f"Unknown candidate_type '{candidate_type}'"}

    # Update candidate status
    store.execute(
        """UPDATE governance.evolution_candidates
           SET review_status = 'accepted', reviewer = %s, review_note = %s, accepted_at = NOW()
           WHERE candidate_id = %s""",
        (reviewer, note, candidate_id),
    )

    # Write review record
    _write_review_record(store, candidate_type, candidate_id, "approve", reviewer, note, candidate)

    # Bump ontology version
    new_version = _bump_version(store, candidate_type, normalized)
    result["ontology_version"] = new_version

    log.info("Approved %s '%s' → version %s", candidate_type, normalized, new_version)
    return result


def reject_candidate(
    candidate_id: str,
    reviewer: str,
    note: str = "",
    *,
    store: RelationalStore,
) -> dict:
    """Reject a candidate."""
    log.info("reject_candidate id=%s reviewer=%s", candidate_id, reviewer)

    candidate = store.fetchone(
        "SELECT * FROM governance.evolution_candidates WHERE candidate_id = %s",
        (candidate_id,),
    )
    if not candidate:
        return {"error": f"Candidate '{candidate_id}' not found"}

    store.execute(
        """UPDATE governance.evolution_candidates
           SET review_status = 'rejected', reviewer = %s, review_note = %s
           WHERE candidate_id = %s""",
        (reviewer, note, candidate_id),
    )

    _write_review_record(
        store, candidate.get("candidate_type", "concept"),
        candidate_id, "reject", reviewer, note, candidate,
    )

    return {"status": "rejected", "candidate_id": candidate_id}


# ── Internal ─────────────────────────────────────────────────────────────────

def _approve_concept(
    candidate: dict, normalized: str, surface_forms: list[str],
    parent_node_id: str | None,
    aliases: list[str],
    *,
    store: RelationalStore, graph: GraphStore, ontology,
) -> dict:
    """Write new concept to Neo4j + OntologyRegistry + lexicon_aliases."""
    # Generate node_id following ontology naming convention:
    # Concept layer: IP.UPPER_SNAKE (e.g. IP.SD_WAN)
    # Use surface_form to derive a readable ID
    raw_name = surface_forms[0] if surface_forms else normalized
    node_id = "IP." + re.sub(r"[^A-Za-z0-9]+", "_", raw_name).upper().strip("_")
    display_name = raw_name

    # Neo4j node
    graph.write(
        """
        MERGE (n:OntologyNode {node_id: $node_id})
        SET n.canonical_name = $name,
            n.lifecycle_state = 'active',
            n.maturity_level = 'evolved',
            n.approved = true,
            n.source_count = $source_count,
            n.composite_score = $composite_score
        """,
        node_id=node_id, name=display_name,
        source_count=int(candidate.get("source_count") or 0),
        composite_score=float(candidate.get("composite_score") or 0),
    )

    # Parent edge
    if parent_node_id:
        graph.write(
            """
            MATCH (child:OntologyNode {node_id: $child_id})
            MATCH (parent:OntologyNode {node_id: $parent_id})
            MERGE (child)-[:SUBCLASS_OF]->(parent)
            """,
            child_id=node_id, parent_id=parent_node_id,
        )

    # Aliases → Neo4j + PG
    for alias in aliases:
        alias_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{node_id}:{alias}"))
        graph.write(
            """
            MERGE (a:Alias {alias_id: $alias_id})
            SET a.surface_form = $form
            WITH a
            MATCH (n:OntologyNode {node_id: $node_id})
            MERGE (a)-[:ALIAS_OF]->(n)
            """,
            alias_id=alias_id, form=alias.lower(), node_id=node_id,
        )
        store.execute(
            """INSERT INTO lexicon_aliases (alias_id, surface_form, canonical_node_id, alias_type, language)
               VALUES (%s, %s, %s, 'evolved', 'en')
               ON CONFLICT (surface_form, canonical_node_id) DO NOTHING""",
            (alias_id, alias.lower(), node_id),
        )

    # Update in-memory OntologyRegistry
    if hasattr(ontology, "alias_map"):
        for alias in aliases:
            ontology.alias_map[alias.lower()] = node_id

    log.info("Concept approved: %s (node_id=%s, parent=%s, aliases=%d)",
             normalized, node_id, parent_node_id, len(aliases))

    return {
        "status": "approved",
        "candidate_type": "concept",
        "node_id": node_id,
        "parent_node_id": parent_node_id,
        "aliases": aliases,
        "needs_backfill": True,
        "backfill_terms": [a.lower() for a in aliases],
    }


def _approve_relation(
    candidate: dict, normalized: str,
    *, store: RelationalStore, graph: GraphStore, ontology,
) -> dict:
    """Add relation type to registry + retroactively create facts from examples."""
    predicate = normalized

    # Update in-memory OntologyRegistry
    if hasattr(ontology, "relation_ids"):
        ontology.relation_ids.add(predicate)

    # Retroactively create facts from stored examples
    examples = candidate.get("examples") or []
    if isinstance(examples, str):
        examples = json.loads(examples)

    facts_created = 0
    for ex in examples:
        subj = ex.get("subject", "")
        obj = ex.get("object", "")
        seg_id = ex.get("segment_id", "")
        doc_id = ex.get("source_doc_id", "")
        if not subj or not obj:
            continue

        fact_id = str(uuid.uuid4())

        # Write fact to PG
        try:
            store.execute(
                """INSERT INTO facts (fact_id, subject, predicate, object, confidence, lifecycle_state, ontology_version)
                   VALUES (%s, %s, %s, %s, 0.65, 'active', 'evolved')
                   ON CONFLICT DO NOTHING""",
                (fact_id, subj, predicate, obj),
            )
            store.execute(
                """INSERT INTO evidence (evidence_id, fact_id, source_doc_id, segment_id,
                       source_rank, extraction_method, evidence_score)
                   VALUES (%s, %s, %s, %s, 'B', 'llm', 0.65)
                   ON CONFLICT DO NOTHING""",
                (str(uuid.uuid4()), fact_id, doc_id or None, seg_id or None),
            )

            # Write to Neo4j with dynamic relationship type
            rel_type = re.sub(r"[^a-zA-Z0-9_]", "_", predicate).upper()
            graph.write(
                f"""
                MERGE (f:Fact {{fact_id: $fact_id}})
                SET f.subject = $subj, f.predicate = $pred, f.object = $obj,
                    f.confidence = 0.65, f.lifecycle_state = 'active'
                WITH f
                MATCH (a:OntologyNode {{node_id: $subj}})
                MATCH (b:OntologyNode {{node_id: $obj}})
                MERGE (a)-[r:{rel_type}]->(b)
                SET r.predicate = $pred,
                    r.confidence = CASE WHEN r.confidence IS NULL OR r.confidence < 0.65
                                   THEN 0.65 ELSE r.confidence END,
                    r.fact_count = coalesce(r.fact_count, 0) + 1
                """,
                fact_id=fact_id, subj=subj, pred=predicate, obj=obj,
            )
            facts_created += 1
        except Exception as exc:
            log.warning("Failed to create fact from example: %s", exc)

    log.info("Relation approved: %s → %d facts retroactively created", predicate, facts_created)
    return {
        "status": "approved",
        "candidate_type": "relation",
        "predicate": predicate,
        "facts_created": facts_created,
        "needs_backfill": False,
    }


def _write_review_record(
    store: RelationalStore, object_type: str, object_id: str,
    action: str, reviewer: str, note: str, before_state: dict,
) -> None:
    store.execute(
        """INSERT INTO governance.review_records
               (review_id, object_type, object_id, action, reviewer, note, before_state)
           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)""",
        (str(uuid.uuid4()), object_type, object_id, action, reviewer, note,
         json.dumps({k: str(v) for k, v in before_state.items()}, default=str)),
    )


def _bump_version(store: RelationalStore, candidate_type: str, name: str) -> str:
    """Bump ontology version (patch increment)."""
    row = store.fetchone(
        "SELECT version_tag FROM governance.ontology_versions ORDER BY created_at DESC LIMIT 1"
    )
    current = row["version_tag"] if row else "v0.2.0"

    # Parse vX.Y.Z → vX.Y.(Z+1)
    parts = current.lstrip("v").split(".")
    if len(parts) == 3:
        parts[2] = str(int(parts[2]) + 1)
    new_version = "v" + ".".join(parts)

    store.execute(
        """INSERT INTO governance.ontology_versions (version_tag, description, status)
           VALUES (%s, %s, 'active')
           ON CONFLICT (version_tag) DO NOTHING""",
        (new_version, f"Approved {candidate_type}: {name}"),
    )

    return new_version