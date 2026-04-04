"""YAMLOntologyProvider — OntologyProvider backed by OntologyRegistry (YAML source)."""

from __future__ import annotations

import logging

from semcore.core.types import KnowledgeLayer, OntologyNode, RelationDef
from semcore.ontology.base import OntologyProvider
from src.ontology.registry import OntologyRegistry

log = logging.getLogger(__name__)


def _node_from_dict(d: dict) -> OntologyNode:
    layer_raw = d.get("knowledge_layer", "concept")
    try:
        layer = KnowledgeLayer(layer_raw)
    except ValueError:
        layer = KnowledgeLayer.CONCEPT
    return OntologyNode(
        node_id=d["id"],
        label=d.get("canonical_name", d["id"]),
        layer=layer,
        domain=d.get("domain", ""),
        aliases=list(d.get("aliases", [])),
        attributes={k: v for k, v in d.items()
                    if k not in {"id", "canonical_name", "knowledge_layer", "domain", "aliases"}},
    )


class YAMLOntologyProvider(OntologyProvider):
    def __init__(self, registry: OntologyRegistry) -> None:
        self._reg = registry

    # ── OntologyProvider ABC ──────────────────────────────────────────────────

    def get_node(self, node_id: str) -> OntologyNode | None:
        d = self._reg.get_node(node_id)
        return _node_from_dict(d) if d else None

    def get_layer_nodes(self, layer: KnowledgeLayer) -> list[OntologyNode]:
        return [_node_from_dict(d) for d in self._reg.get_layer_nodes(layer.value)]

    def get_all_nodes(self) -> list[OntologyNode]:
        return [_node_from_dict(d) for d in self._reg.nodes.values()]

    def get_relations(self) -> list[RelationDef]:
        return [RelationDef(id=rid, label=rid) for rid in self._reg.relation_ids]

    def resolve_alias(
        self, surface_form: str, *, lang: str = "en", domain: str | None = None
    ) -> OntologyNode | None:
        nid = self._reg.lookup_alias(surface_form)
        if nid is None:
            return None
        d = self._reg.get_node(nid)
        return _node_from_dict(d) if d else None

    def version(self) -> str:
        from src.config.settings import settings
        return settings.ONTOLOGY_VERSION

    # ── Pass-through helpers for stages that still use registry directly ──────

    def lookup_alias(self, surface_form: str) -> str | None:
        return self._reg.lookup_alias(surface_form)

    def is_valid_relation(self, relation_id: str) -> bool:
        return self._reg.is_valid_relation(relation_id)

    def all_node_ids(self) -> list[str]:
        return self._reg.all_node_ids()

    def get_node_dict(self, node_id: str) -> dict | None:
        """Return raw dict for stages that need the full YAML structure."""
        return self._reg.get_node(node_id)

    def get_layer_node_dicts(self, layer: str) -> list[dict]:
        return self._reg.get_layer_nodes(layer)

    def get_node_layer(self, node_id: str) -> str:
        return self._reg.get_node_layer(node_id)

    @property
    def alias_map(self) -> dict[str, str]:
        """Read-only alias lookup table: lower(surface_form) → node_id."""
        return self._reg.alias_map

    @property
    def relation_ids(self) -> set[str]:
        """Set of valid relation type identifiers."""
        return self._reg.relation_ids

    @property
    def nodes(self) -> dict[str, dict]:
        """All ontology nodes keyed by node_id (raw dict form)."""
        return self._reg.nodes

    @property
    def seed_relations(self):
        return self._reg.seed_relations

    @property
    def classification_fixes(self):
        return self._reg.classification_fixes

    @property
    def semantic_role_patterns(self):
        return self._reg.semantic_role_patterns

    @property
    def context_signal_patterns(self):
        return self._reg.context_signal_patterns

    @property
    def predicate_signal_patterns(self):
        return self._reg.predicate_signal_patterns