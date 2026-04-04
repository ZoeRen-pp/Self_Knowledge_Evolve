"""
In-memory ontology registry.
Loads all YAML files at startup; provides fast lookup for pipeline alignment.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

log = logging.getLogger(__name__)


class OntologyRegistry:
    def __init__(self, ontology_root: Path) -> None:
        self.nodes: dict[str, dict] = {}           # node_id → node dict
        self.alias_map: dict[str, str] = {}        # lower(surface_form) → node_id
        self.relation_ids: set[str] = set()        # valid relation type ids
        self._layer_index: dict[str, list[str]] = {}  # knowledge_layer → [node_id]

        # Seed relations loaded from ontology/seeds/*.yaml
        self.seed_relations: list[dict] = []             # [{subject, predicate, object, note?}]
        self.classification_fixes: list[dict] = []       # [{node_id, action, new_parent, ...}]

        # Compiled pattern lists loaded from ontology/patterns/*.yaml
        self.semantic_role_patterns: list[tuple[re.Pattern, str]] = []
        self.context_signal_patterns: list[tuple[re.Pattern, str]] = []
        self.relation_extraction_patterns: list[tuple[re.Pattern, str]] = []
        self.predicate_signal_patterns: list[tuple[re.Pattern, str]] = []

        self._load_relations(ontology_root / "top" / "relations.yaml")
        for domain_file in sorted((ontology_root / "domains").glob("*.yaml")):
            self._load_domain(domain_file)
        alias_file = ontology_root / "lexicon" / "aliases.yaml"
        if alias_file.exists():
            self._load_aliases(alias_file)
        self._load_patterns(ontology_root / "patterns")
        self._load_seeds(ontology_root / "seeds")

        pattern_counts = {
            "semantic_roles": len(self.semantic_role_patterns),
            "context_signals": len(self.context_signal_patterns),
            "relation_extraction": len(self.relation_extraction_patterns),
            "predicate_signals": len(self.predicate_signal_patterns),
        }
        log.info(
            "OntologyRegistry loaded: %d nodes, %d aliases, %d relations, "
            "layers=%s, patterns=%s, seeds=%d, fixes=%d",
            len(self.nodes), len(self.alias_map), len(self.relation_ids),
            {k: len(v) for k, v in self._layer_index.items()},
            pattern_counts,
            len(self.seed_relations), len(self.classification_fixes),
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
            # Build knowledge_layer index
            layer = node.get("knowledge_layer", "concept")
            self._layer_index.setdefault(layer, []).append(nid)

    def _load_aliases(self, path: Path) -> None:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data.get("aliases", []):
            sf = entry["surface_form"].lower()
            self.alias_map[sf] = entry["canonical_node_id"]

    def _load_seeds(self, seeds_dir: Path) -> None:
        """Load seed relations and classification fixes from ontology/seeds/*.yaml."""
        if not seeds_dir.is_dir():
            log.debug("No seeds directory at %s", seeds_dir)
            return

        for seed_file in sorted(seeds_dir.glob("*.yaml")):
            data = yaml.safe_load(seed_file.read_text(encoding="utf-8")) or {}

            for rel in data.get("relations", []):
                subj = rel.get("subject", "")
                pred = rel.get("predicate", "")
                obj = rel.get("object", "")
                if subj and pred and obj:
                    self.seed_relations.append({
                        "subject": subj,
                        "predicate": pred,
                        "object": obj,
                        "note": rel.get("note", ""),
                        "source_file": seed_file.name,
                    })

            for fix in data.get("fixes", []):
                self.classification_fixes.append(fix)

        log.debug("Loaded %d seed relations, %d fixes from %s",
                  len(self.seed_relations), len(self.classification_fixes), seeds_dir)

    def _load_patterns(self, patterns_dir: Path) -> None:
        """Load and compile regex patterns from ontology/patterns/*.yaml."""
        if not patterns_dir.is_dir():
            log.debug("No patterns directory at %s, using empty pattern sets", patterns_dir)
            return

        self.semantic_role_patterns = self._compile_role_patterns(
            patterns_dir / "semantic_roles.yaml"
        )
        self.context_signal_patterns = self._compile_signal_patterns(
            patterns_dir / "context_signals.yaml"
        )
        self.relation_extraction_patterns = self._compile_relation_patterns(
            patterns_dir / "relation_extraction.yaml"
        )
        self.predicate_signal_patterns = self._compile_signal_patterns(
            patterns_dir / "predicate_signals.yaml"
        )

    def _compile_role_patterns(self, path: Path) -> list[tuple[re.Pattern, str]]:
        """Load semantic_roles.yaml → [(compiled_regex, role_type)]."""
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result = []
        for role in data.get("roles", []):
            role_type = role["type"]
            flags = self._parse_flags(role.get("flags", ""))
            for pattern_str in role.get("patterns", []):
                try:
                    result.append((re.compile(pattern_str, flags), role_type))
                except re.error as exc:
                    log.warning("Invalid regex in %s type=%s: %s", path.name, role_type, exc)
        return result

    def _compile_signal_patterns(self, path: Path) -> list[tuple[re.Pattern, str]]:
        """Load signal YAML (context_signals or predicate_signals) → [(compiled_regex, label/predicate)]."""
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result = []
        for entry in data.get("signals", []):
            label = entry.get("label") or entry.get("predicate", "")
            flags = self._parse_flags(entry.get("flags", ""))
            for pattern_str in entry.get("patterns", []):
                try:
                    result.append((re.compile(pattern_str, flags), label))
                except re.error as exc:
                    log.warning("Invalid regex in %s label=%s: %s", path.name, label, exc)
        return result

    def _compile_relation_patterns(self, path: Path) -> list[tuple[re.Pattern, str]]:
        """Load relation_extraction.yaml → [(compiled_regex, predicate)]."""
        if not path.exists():
            return []
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        result = []
        for entry in data.get("relations", []):
            predicate = entry["predicate"]
            flags = self._parse_flags(entry.get("flags", ""))
            try:
                result.append((re.compile(entry["pattern"], flags), predicate))
            except re.error as exc:
                log.warning("Invalid regex in %s predicate=%s: %s", path.name, predicate, exc)
        return result

    @staticmethod
    def _parse_flags(flags_str: str) -> int:
        """Parse flag string like 'IGNORECASE|MULTILINE' into re flags."""
        if not flags_str:
            return re.IGNORECASE
        flag_map = {"IGNORECASE": re.I, "MULTILINE": re.M, "DOTALL": re.S}
        result = 0
        for name in flags_str.split("|"):
            result |= flag_map.get(name.strip().upper(), 0)
        return result or re.IGNORECASE

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

    def get_layer_nodes(self, knowledge_layer: str) -> list[dict]:
        """Return all nodes belonging to a given knowledge layer.

        Args:
            knowledge_layer: One of 'concept', 'mechanism', 'method',
                             'condition', 'scenario'.
        """
        return [self.nodes[nid] for nid in self._layer_index.get(knowledge_layer, [])
                if nid in self.nodes]

    def get_node_layer(self, node_id: str) -> str:
        """Return the knowledge_layer of a node, defaulting to 'concept'."""
        node = self.nodes.get(node_id)
        if node is None:
            return "concept"
        return node.get("knowledge_layer", "concept")

    # ── Factory ───────────────────────────────────────────────────

    _default_instance: "OntologyRegistry | None" = None

    @classmethod
    def from_default(cls) -> "OntologyRegistry":
        """Load from ./ontology relative to CWD (cached singleton)."""
        if cls._default_instance is None:
            cls._default_instance = cls(Path("ontology"))
        return cls._default_instance

    @classmethod
    def reset_default(cls) -> None:
        """Clear cached singleton (for testing)."""
        cls._default_instance = None
