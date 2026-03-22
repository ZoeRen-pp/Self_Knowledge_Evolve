"""
In-memory ontology registry.
Loads all YAML files at startup; provides fast lookup for pipeline alignment.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


class OntologyRegistry:
    def __init__(self, ontology_root: Path) -> None:
        self.nodes: dict[str, dict] = {}           # node_id → node dict
        self.alias_map: dict[str, str] = {}        # lower(surface_form) → node_id
        self.relation_ids: set[str] = set()        # valid relation type ids

        self._load_relations(ontology_root / "top" / "relations.yaml")
        for domain_file in sorted((ontology_root / "domains").glob("*.yaml")):
            self._load_domain(domain_file)
        alias_file = ontology_root / "lexicon" / "aliases.yaml"
        if alias_file.exists():
            self._load_aliases(alias_file)

        log.info(
            "OntologyRegistry loaded: %d nodes, %d aliases, %d relations",
            len(self.nodes), len(self.alias_map), len(self.relation_ids),
        )

    # ── Loaders ──────────────────────────────────────────────────

    def _load_relations(self, path: Path) -> None:
        if not path.exists():
            log.warning("relations.yaml not found at %s", path)
            return
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for rel in data.get("relations", []):
            self.relation_ids.add(rel["id"])

    def _load_domain(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for node in data.get("nodes", []):
            nid = node["id"]
            self.nodes[nid] = node
            # Index canonical name
            self.alias_map[node["canonical_name"].lower()] = nid
            # Index display_name_zh if present
            if node.get("display_name_zh"):
                self.alias_map[node["display_name_zh"].lower()] = nid
            # Index inline aliases list
            for alias in node.get("aliases", []):
                self.alias_map[alias.lower()] = nid

    def _load_aliases(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data.get("aliases", []):
            sf = entry["surface_form"].lower()
            self.alias_map[sf] = entry["canonical_node_id"]

    # ── Public API ────────────────────────────────────────────────

    def get_node(self, node_id: str) -> dict | None:
        return self.nodes.get(node_id)

    def lookup_alias(self, surface_form: str) -> str | None:
        """Return node_id for surface_form (case-insensitive), or None."""
        return self.alias_map.get(surface_form.lower())

    def is_valid_relation(self, relation_id: str) -> bool:
        return relation_id in self.relation_ids

    def all_node_ids(self) -> list[str]:
        return list(self.nodes.keys())

    def get_domain_nodes(self, domain_prefix: str) -> list[dict]:
        """Return all nodes whose id starts with the given prefix (e.g. 'IP')."""
        prefix = domain_prefix.upper() + "."
        return [n for nid, n in self.nodes.items() if nid.startswith(prefix)]

    # ── Factory ───────────────────────────────────────────────────

    @classmethod
    def from_default(cls) -> "OntologyRegistry":
        """Load from ./ontology relative to CWD."""
        return cls(Path("ontology"))
