"""Data structures for the query engine working memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


RESERVED_EDGES = frozenset({"tagged_in", "rst_adjacent", "evidenced_by"})

VALID_OPS = frozenset({"seed", "expand", "combine", "aggregate", "project"})
SEED_BY_MODES = frozenset({"id", "alias", "layer", "embedding", "attribute"})
TARGET_TYPES = frozenset({"node", "segment", "fact"})
COMBINE_METHODS = frozenset({"union", "intersect", "subtract"})
AGG_FUNCTIONS = frozenset({"count", "rank", "group", "score", "rerank"})
DIRECTIONS = frozenset({"outbound", "inbound", "both"})

MAX_STEPS = 20
MAX_DEPTH = 10
MAX_TOP_K = 500
MAX_RESULT_SET = 5000
MAX_TRAVERSE_NODES = 10_000
MAX_TOTAL_SECONDS = 30.0
MAX_STEP_SECONDS = 5.0
MAX_CROSS_ENCODER = 50


@dataclass
class NodeRef:
    node_id: str
    node_type: str
    properties: dict[str, Any] = field(default_factory=dict)

    def __hash__(self) -> int:
        return hash((self.node_id, self.node_type))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, NodeRef):
            return NotImplemented
        return self.node_id == other.node_id and self.node_type == other.node_type


@dataclass
class StepTrace:
    step_index: int
    op: str
    as_var: str
    result_size: int = 0
    ms: float = 0.0


@dataclass
class ResultSet:
    nodes: list[NodeRef] = field(default_factory=list)
    provenance: list[StepTrace] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def node_ids(self) -> set[str]:
        return {n.node_id for n in self.nodes}

    def truncate(self, limit: int) -> bool:
        if len(self.nodes) <= limit:
            return False
        self.nodes = self.nodes[:limit]
        self.metadata["truncated"] = True
        return True


class WorkingMemory:
    def __init__(self) -> None:
        self._slots: dict[str, ResultSet] = {}

    def get(self, var: str) -> ResultSet:
        return self._slots[var]

    def put(self, var: str, rs: ResultSet) -> None:
        self._slots[var] = rs

    def has(self, var: str) -> bool:
        return var in self._slots

    def all_vars(self) -> dict[str, ResultSet]:
        return dict(self._slots)