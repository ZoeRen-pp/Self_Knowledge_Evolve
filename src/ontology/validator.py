"""Validates ontology YAML files for structural consistency."""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

REQUIRED_NODE_FIELDS = {"id", "canonical_name", "maturity_level", "lifecycle_state"}
VALID_MATURITY = {"core", "extended", "experimental"}
VALID_LIFECYCLE = {"active", "deprecated"}


def validate_domain_file(path: Path, relation_ids: set[str] | None = None) -> list[str]:
    """Return list of error messages; empty list means valid."""
    errors: list[str] = []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    nodes = data.get("nodes", [])
    node_ids = {n["id"] for n in nodes if isinstance(n, dict) and "id" in n}

    for node in nodes:
        if not isinstance(node, dict):
            errors.append(f"{path.name}: non-dict node entry found")
            continue

        nid = node.get("id", "<missing>")

        # Required fields
        missing = REQUIRED_NODE_FIELDS - set(node.keys())
        if missing:
            errors.append(f"{path.name}:{nid}: missing fields {missing}")

        # Maturity level
        if node.get("maturity_level") not in VALID_MATURITY:
            errors.append(
                f"{path.name}:{nid}: invalid maturity_level '{node.get('maturity_level')}'"
            )

        # Lifecycle state
        if node.get("lifecycle_state") not in VALID_LIFECYCLE:
            errors.append(
                f"{path.name}:{nid}: invalid lifecycle_state '{node.get('lifecycle_state')}'"
            )

        # Parent must exist in same file (or be null for top-level)
        parent = node.get("parent_id")
        if parent and parent not in node_ids:
            errors.append(
                f"{path.name}:{nid}: parent_id '{parent}' not found in same file"
            )

        # Allowed relations must be valid
        if relation_ids is not None:
            for rel in node.get("allowed_relations", []):
                if rel not in relation_ids:
                    errors.append(
                        f"{path.name}:{nid}: allowed_relation '{rel}' not in relations.yaml"
                    )

    return errors


def validate_all(ontology_root: Path) -> bool:
    """Validate all domain files. Prints errors. Returns True if all pass."""
    relations_path = ontology_root / "top" / "relations.yaml"
    relation_ids: set[str] | None = None
    if relations_path.exists():
        data = yaml.safe_load(relations_path.read_text(encoding="utf-8")) or {}
        relation_ids = {r["id"] for r in data.get("relations", [])}

    all_ok = True
    for domain_file in sorted((ontology_root / "domains").glob("*.yaml")):
        errs = validate_domain_file(domain_file, relation_ids)
        if errs:
            all_ok = False
            for e in errs:
                log.error(e)
        else:
            log.info("✓ %s", domain_file.name)

    return all_ok