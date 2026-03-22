"""
Load ontology YAML files into Neo4j and PostgreSQL lexicon_aliases.

Run order:
    1. ontology/top/relations.yaml      → stored in-memory / PG config only (no graph nodes)
    2. ontology/domains/*.yaml          → OntologyNode nodes in Neo4j
    3. ontology/lexicon/aliases.yaml    → Alias nodes in Neo4j + rows in PG lexicon_aliases

Usage:
    python scripts/load_ontology.py                   # load all
    python scripts/load_ontology.py --domain ip       # load one domain file
    python scripts/load_ontology.py --dry-run         # validate only
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

sys.path.insert(0, ".")

from src.config.settings import settings
from src.db.neo4j_client import get_session, ping as neo4j_ping
from src.db.postgres import execute, ping as pg_ping

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

ONTOLOGY_ROOT = Path("ontology")
DOMAIN_FILES  = list((ONTOLOGY_ROOT / "domains").glob("*.yaml"))
ALIAS_FILE    = ONTOLOGY_ROOT / "lexicon" / "aliases.yaml"


# ─────────────────────────────────────────────────────────────────────────────
# Neo4j loaders
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_ONTOLOGY_NODE = """
MERGE (n:OntologyNode {node_id: $node_id})
SET
  n.canonical_name     = $canonical_name,
  n.display_name_zh    = $display_name_zh,
  n.domain             = $domain,
  n.subdomain          = $subdomain,
  n.description        = $description,
  n.maturity_level     = $maturity_level,
  n.lifecycle_state    = $lifecycle_state,
  n.version_introduced = $version_introduced,
  n.source_basis       = $source_basis,
  n.allowed_relations  = $allowed_relations
"""

UPSERT_SUBCLASS_EDGE = """
MATCH (child:OntologyNode {node_id: $child_id})
MATCH (parent:OntologyNode {node_id: $parent_id})
MERGE (child)-[r:SUBCLASS_OF]->(parent)
SET r.ontology_version = $version
"""

UPSERT_ALIAS_NODE = """
MERGE (a:Alias {alias_id: $alias_id})
SET
  a.surface_form     = $surface_form,
  a.alias_type       = $alias_type,
  a.vendor           = $vendor,
  a.language         = $language,
  a.confidence       = $confidence,
  a.ontology_version = $ontology_version
WITH a
MATCH (n:OntologyNode {node_id: $canonical_node_id})
MERGE (a)-[r:ALIAS_OF]->(n)
SET r.ontology_version = $ontology_version
"""


def load_domain_file(path: Path, session, dry_run: bool) -> int:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    version = data.get("version", settings.ONTOLOGY_VERSION)
    nodes = data.get("nodes", [])
    count = 0

    for node in nodes:
        params = {
            "node_id":          node["id"],
            "canonical_name":   node.get("canonical_name", ""),
            "display_name_zh":  node.get("display_name_zh", ""),
            "domain":           data.get("domain", ""),
            "subdomain":        node.get("subdomain", ""),
            "description":      node.get("description", ""),
            "maturity_level":   node.get("maturity_level", "extended"),
            "lifecycle_state":  node.get("lifecycle_state", "active"),
            "version_introduced": node.get("version_introduced", version),
            "source_basis":     node.get("source_basis", []),
            "allowed_relations":node.get("allowed_relations", []),
        }
        if not dry_run:
            session.run(UPSERT_ONTOLOGY_NODE, **params)
        log.info("  OntologyNode: %s", node["id"])
        count += 1

    # Build SUBCLASS_OF edges after all nodes exist
    for node in nodes:
        parent_id = node.get("parent_id")
        if parent_id and not dry_run:
            session.run(
                UPSERT_SUBCLASS_EDGE,
                child_id=node["id"],
                parent_id=parent_id,
                version=version,
            )

    return count


def load_aliases(path: Path, session, dry_run: bool) -> int:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    version = data.get("version", settings.ONTOLOGY_VERSION)
    aliases = data.get("aliases", [])
    count = 0

    for idx, alias in enumerate(aliases):
        alias_id = f"alias-{alias['canonical_node_id']}-{idx}"
        params = {
            "alias_id":         alias_id,
            "surface_form":     alias["surface_form"],
            "canonical_node_id":alias["canonical_node_id"],
            "alias_type":       alias.get("alias_type", "alternate_spelling"),
            "vendor":           alias.get("vendor", ""),
            "language":         alias.get("language", "en"),
            "confidence":       alias.get("confidence", 1.0),
            "ontology_version": version,
        }
        if not dry_run:
            # Neo4j
            session.run(UPSERT_ALIAS_NODE, **params)
            # PostgreSQL mirror
            execute(
                """
                INSERT INTO lexicon_aliases
                  (alias_id, surface_form, canonical_node_id, alias_type,
                   vendor, language, confidence, ontology_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (surface_form, canonical_node_id) DO NOTHING
                """,
                (
                    alias_id,
                    alias["surface_form"],
                    alias["canonical_node_id"],
                    alias.get("alias_type", "alternate_spelling"),
                    alias.get("vendor", ""),
                    alias.get("language", "en"),
                    alias.get("confidence", 1.0),
                    version,
                ),
            )
        log.info("  Alias: %s → %s", alias["surface_form"], alias["canonical_node_id"])
        count += 1

    return count


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Load ontology into Neo4j/PG")
    parser.add_argument("--domain", help="Load only this domain file (e.g. 'ip')")
    parser.add_argument("--dry-run", action="store_true", help="Validate without writing")
    args = parser.parse_args()

    if not neo4j_ping():
        log.error("Neo4j unreachable. Check NEO4J_URI in .env")
        sys.exit(1)
    if not pg_ping():
        log.error("PostgreSQL unreachable. Check POSTGRES_* in .env")
        sys.exit(1)

    domain_files = DOMAIN_FILES
    if args.domain:
        domain_files = [f for f in DOMAIN_FILES if args.domain in f.stem]
        if not domain_files:
            log.error("No domain file matched '%s'", args.domain)
            sys.exit(1)

    total_nodes = 0
    total_aliases = 0

    with get_session() as session:
        for path in sorted(domain_files):
            log.info("Loading domain: %s", path)
            total_nodes += load_domain_file(path, session, args.dry_run)

        if ALIAS_FILE.exists():
            log.info("Loading aliases: %s", ALIAS_FILE)
            with get_session() as alias_session:
                total_aliases += load_aliases(ALIAS_FILE, alias_session, args.dry_run)

    log.info(
        "Done%s — %d OntologyNodes, %d Aliases",
        " (dry-run)" if args.dry_run else "",
        total_nodes,
        total_aliases,
    )


if __name__ == "__main__":
    main()
