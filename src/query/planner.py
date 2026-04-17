"""QueryPlanner — variable dependency analysis → execution waves."""

from __future__ import annotations

from typing import Any


class QueryPlanner:
    def plan(self, steps: list[dict[str, Any]]) -> list[list[int]]:
        """Return execution waves: each wave is a list of step indices that can run in parallel."""
        deps: dict[int, set[int]] = {}
        var_to_step: dict[str, int] = {}

        for i, step in enumerate(steps):
            as_var = step.get("as", "")
            var_to_step[as_var] = i
            deps[i] = set()

        for i, step in enumerate(steps):
            refs = self._extract_refs(step)
            for ref in refs:
                if ref in var_to_step:
                    deps[i].add(var_to_step[ref])

        assigned: dict[int, int] = {}
        waves: list[list[int]] = []
        remaining = set(range(len(steps)))

        while remaining:
            ready = []
            for idx in remaining:
                if all(d in assigned for d in deps[idx]):
                    ready.append(idx)
            if not ready:
                raise ValueError("Circular dependency detected in query plan")
            wave_num = len(waves)
            for idx in ready:
                assigned[idx] = wave_num
                remaining.discard(idx)
            waves.append(sorted(ready))

        return waves

    def _extract_refs(self, step: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        from_var = step.get("from")
        if isinstance(from_var, str) and from_var.startswith("$"):
            refs.append(from_var)
        sets_val = step.get("sets")
        if isinstance(sets_val, list):
            for s in sets_val:
                if isinstance(s, str) and s.startswith("$"):
                    refs.append(s)
        return refs