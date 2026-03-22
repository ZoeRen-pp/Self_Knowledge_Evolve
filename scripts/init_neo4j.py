"""
Neo4j schema initialisation — create constraints and indexes.

Run once after the container is up:
    python scripts/init_neo4j.py
"""

import logging
import sys

sys.path.insert(0, ".")  # run from project root

from src.db.neo4j_client import get_session, ping

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Uniqueness constraints (also create backing indexes) ──────────────────────
CONSTRAINTS = [
    ("OntologyNode",      "node_id"),
    ("Concept",           "concept_id"),
    ("Entity",            "entity_id"),
    ("Fact",              "fact_id"),
    ("KnowledgeSegment",  "segment_id"),
    ("SourceDocument",    "source_doc_id"),
    ("Evidence",          "evidence_id"),
    ("Alias",             "alias_id"),
    ("CandidateConcept",  "candidate_id"),
    ("OntologyVersion",   "version_tag"),
]

# ── Additional lookup indexes (non-unique) ────────────────────────────────────
INDEXES = [
    ("OntologyNode",     "domain"),
    ("OntologyNode",     "lifecycle_state"),
    ("Concept",          "ontology_node_id"),
    ("Concept",          "lifecycle_state"),
    ("Fact",             "subject"),
    ("Fact",             "predicate"),
    ("Fact",             "object"),
    ("Fact",             "lifecycle_state"),
    ("KnowledgeSegment", "source_doc_id"),
    ("KnowledgeSegment", "segment_type"),
    ("Alias",            "surface_form"),
    ("CandidateConcept", "review_status"),
    ("CandidateConcept", "composite_score"),
]


def create_constraints(session) -> None:
    for label, prop in CONSTRAINTS:
        name = f"uq_{label.lower()}_{prop}"
        cypher = (
            f"CREATE CONSTRAINT {name} IF NOT EXISTS "
            f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
        )
        session.run(cypher)
        log.info("Constraint ensured: %s", name)


def create_indexes(session) -> None:
    for label, prop in INDEXES:
        name = f"idx_{label.lower()}_{prop}"
        cypher = (
            f"CREATE INDEX {name} IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.{prop})"
        )
        session.run(cypher)
        log.info("Index ensured: %s", name)


def main() -> None:
    if not ping():
        log.error("Cannot reach Neo4j — check NEO4J_URI / credentials in .env")
        sys.exit(1)

    with get_session() as session:
        log.info("Creating uniqueness constraints …")
        create_constraints(session)
        log.info("Creating lookup indexes …")
        create_indexes(session)

    log.info("Neo4j schema initialisation complete.")


if __name__ == "__main__":
    main()
