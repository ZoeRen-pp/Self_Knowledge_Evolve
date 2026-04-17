"""QueryValidator — static checks on query plans before execution."""

from __future__ import annotations

import re
from typing import Any

from src.query.types import (
    AGG_FUNCTIONS,
    COMBINE_METHODS,
    DIRECTIONS,
    MAX_DEPTH,
    MAX_STEPS,
    MAX_TOP_K,
    RESERVED_EDGES,
    SEED_BY_MODES,
    TARGET_TYPES,
    VALID_OPS,
)

_VAR_RE = re.compile(r"^\$[a-z_][a-z0-9_]*$")


class ValidationError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class QueryValidator:
    def __init__(self, valid_relation_ids: set[str]) -> None:
        self._relation_ids = valid_relation_ids

    def validate(self, plan: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        steps = plan.get("steps")
        if not isinstance(steps, list) or not steps:
            return ["'steps' must be a non-empty list"]
        if len(steps) > MAX_STEPS:
            errors.append(f"Too many steps: {len(steps)} > {MAX_STEPS}")

        declared: set[str] = set()
        for i, step in enumerate(steps):
            pfx = f"step[{i}]"
            if not isinstance(step, dict):
                errors.append(f"{pfx}: must be a dict")
                continue

            op = step.get("op")
            if op not in VALID_OPS:
                errors.append(f"{pfx}: unknown op '{op}'")
                continue

            as_var = step.get("as")
            if not as_var or not _VAR_RE.match(str(as_var)):
                errors.append(f"{pfx}: 'as' must match $var_name pattern")
            elif as_var in declared:
                errors.append(f"{pfx}: duplicate variable '{as_var}'")
            else:
                declared.add(as_var)

            validator = getattr(self, f"_check_{op}", None)
            if validator:
                validator(step, i, declared, errors)

        return errors

    def _check_ref(self, var: str, step_idx: int, declared: set[str], errors: list[str]) -> None:
        if not isinstance(var, str) or not var.startswith("$"):
            errors.append(f"step[{step_idx}]: variable reference must start with '$'")
        elif var not in declared:
            errors.append(f"step[{step_idx}]: undefined variable '{var}'")

    def _check_seed(self, step: dict, idx: int, declared: set[str], errors: list[str]) -> None:
        pfx = f"step[{idx}]"
        by = step.get("by")
        if by not in SEED_BY_MODES:
            errors.append(f"{pfx}: seed.by must be one of {sorted(SEED_BY_MODES)}")
        target = step.get("target")
        if target not in TARGET_TYPES:
            errors.append(f"{pfx}: seed.target must be one of {sorted(TARGET_TYPES)}")
        if "value" not in step:
            errors.append(f"{pfx}: seed requires 'value'")
        if by == "embedding":
            top_k = step.get("top_k", 100)
            if not isinstance(top_k, int) or top_k < 1 or top_k > MAX_TOP_K:
                errors.append(f"{pfx}: top_k must be 1..{MAX_TOP_K}")

    def _check_expand(self, step: dict, idx: int, declared: set[str], errors: list[str]) -> None:
        pfx = f"step[{idx}]"
        from_var = step.get("from")
        if from_var:
            self._check_ref(from_var, idx, declared, errors)

        has_any = "any_of" in step
        has_seq = "sequence" in step
        if has_any and has_seq:
            errors.append(f"{pfx}: any_of and sequence are mutually exclusive")
        if not has_any and not has_seq:
            errors.append(f"{pfx}: expand requires 'any_of' or 'sequence'")

        edge_list = step.get("any_of") or step.get("sequence") or []
        if not isinstance(edge_list, list) or not edge_list:
            errors.append(f"{pfx}: edge type list must be non-empty")
        else:
            for e in edge_list:
                if e not in self._relation_ids and e not in RESERVED_EDGES:
                    errors.append(f"{pfx}: unknown edge type '{e}'")

        depth = step.get("depth", 1)
        if depth != "unlimited":
            if not isinstance(depth, int) or depth < 1 or depth > MAX_DEPTH:
                errors.append(f"{pfx}: depth must be 1..{MAX_DEPTH} or 'unlimited'")

        decay = step.get("confidence_decay")
        if decay is not None:
            if not isinstance(decay, (int, float)) or decay <= 0 or decay > 1:
                errors.append(f"{pfx}: confidence_decay must be in (0, 1]")

        direction = step.get("direction", "outbound")
        if direction not in DIRECTIONS:
            errors.append(f"{pfx}: direction must be one of {sorted(DIRECTIONS)}")

        target = step.get("target")
        if target is not None and target not in TARGET_TYPES:
            errors.append(f"{pfx}: target must be one of {sorted(TARGET_TYPES)}")

    def _check_combine(self, step: dict, idx: int, declared: set[str], errors: list[str]) -> None:
        pfx = f"step[{idx}]"
        method = step.get("method")
        if method not in COMBINE_METHODS:
            errors.append(f"{pfx}: combine.method must be one of {sorted(COMBINE_METHODS)}")
        sets = step.get("sets")
        if not isinstance(sets, list) or len(sets) < 2:
            errors.append(f"{pfx}: combine.sets requires >= 2 variables")
        else:
            if method == "subtract" and len(sets) != 2:
                errors.append(f"{pfx}: subtract requires exactly 2 sets")
            for s in sets:
                self._check_ref(s, idx, declared, errors)

    def _check_aggregate(self, step: dict, idx: int, declared: set[str], errors: list[str]) -> None:
        pfx = f"step[{idx}]"
        from_var = step.get("from")
        if from_var:
            self._check_ref(from_var, idx, declared, errors)
        func = step.get("function")
        if func not in AGG_FUNCTIONS:
            errors.append(f"{pfx}: aggregate.function must be one of {sorted(AGG_FUNCTIONS)}")
        limit = step.get("limit")
        if limit is not None:
            if not isinstance(limit, int) or limit < 1 or limit > MAX_TOP_K:
                errors.append(f"{pfx}: limit must be 1..{MAX_TOP_K}")

    def _check_project(self, step: dict, idx: int, declared: set[str], errors: list[str]) -> None:
        pfx = f"step[{idx}]"
        from_var = step.get("from")
        if from_var:
            self._check_ref(from_var, idx, declared, errors)
        fields = step.get("fields")
        if not isinstance(fields, list) or not fields:
            errors.append(f"{pfx}: project.fields must be a non-empty list")