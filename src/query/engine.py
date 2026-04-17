"""QueryExecutor — orchestrates validator → planner → wave execution."""

from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

from src.query.types import (
    MAX_TOTAL_SECONDS,
    WorkingMemory,
    StepTrace,
    ResultSet,
)
from src.query.validator import QueryValidator, ValidationError
from src.query.planner import QueryPlanner
from src.query.executors import (
    SeedExecutor,
    ExpandExecutor,
    CombineExecutor,
    AggregateExecutor,
    ProjectExecutor,
)

if TYPE_CHECKING:
    from semcore.app import SemanticApp

log = logging.getLogger(__name__)

_EXECUTORS = {
    "seed": SeedExecutor(),
    "expand": ExpandExecutor(),
    "combine": CombineExecutor(),
    "aggregate": AggregateExecutor(),
    "project": ProjectExecutor(),
}


class QueryEngine:
    def __init__(self, app: SemanticApp) -> None:
        self._app = app
        relation_ids = set()
        try:
            relation_ids = set(app.ontology.relation_ids)
        except Exception:
            pass
        self._validator = QueryValidator(relation_ids)
        self._planner = QueryPlanner()

    def execute(self, plan: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()

        errors = self._validator.validate(plan)
        if errors:
            raise ValidationError(errors)

        steps: list[dict[str, Any]] = plan["steps"]
        waves = self._planner.plan(steps)

        wm = WorkingMemory()
        steps_detail: list[dict[str, Any]] = []
        timed_out = False

        for wave in waves:
            if timed_out:
                break
            for idx in wave:
                elapsed = time.time() - t0
                if elapsed > MAX_TOTAL_SECONDS:
                    timed_out = True
                    log.warning("Query timeout after %.1fs at step %d", elapsed, idx)
                    break

                step = steps[idx]
                op = step["op"]
                as_var = step.get("as", "")
                step_t0 = time.time()

                try:
                    executor = _EXECUTORS[op]
                    if op == "aggregate":
                        executor.execute(step, idx, wm, self._app, plan)
                    else:
                        executor.execute(step, idx, wm, self._app)
                except Exception:
                    log.exception("Step %d (%s) failed", idx, op)
                    wm.put(as_var, ResultSet(
                        provenance=[StepTrace(idx, op, as_var)],
                        metadata={"error": True},
                    ))

                step_ms = (time.time() - step_t0) * 1000
                rs = wm.get(as_var) if wm.has(as_var) else ResultSet()
                result_size = rs.metadata.get("value", len(rs.nodes)) if "value" in rs.metadata else len(rs.nodes)
                steps_detail.append({
                    "step": idx,
                    "op": op,
                    "as": as_var,
                    "result_size": result_size,
                    "ms": round(step_ms, 1),
                })

        total_ms = (time.time() - t0) * 1000

        last_var = steps[-1].get("as", "")
        result_data = self._serialize_result(wm, last_var)

        return {
            "meta": {
                "ontology_version": self._app.ontology.version(),
                "latency_ms": round(total_ms, 1),
                "steps_executed": len(steps_detail),
                "timeout": timed_out,
                "steps_detail": steps_detail,
            },
            "result": result_data,
        }

    def _serialize_result(self, wm: WorkingMemory, last_var: str) -> dict[str, Any]:
        output: dict[str, Any] = {}
        if last_var and wm.has(last_var):
            rs = wm.get(last_var)
            output[last_var] = self._serialize_rs(rs)

        for var, rs in wm.all_vars().items():
            if var != last_var:
                if "value" in rs.metadata:
                    output[var] = {"value": rs.metadata["value"]}
                else:
                    output[var] = {
                        "count": len(rs.nodes),
                        "truncated": rs.metadata.get("truncated", False),
                    }
        return output

    def _serialize_rs(self, rs: ResultSet) -> list[dict[str, Any]] | dict[str, Any]:
        if "value" in rs.metadata:
            return {"value": rs.metadata["value"]}
        return [
            {"node_id": n.node_id, "node_type": n.node_type, **n.properties}
            for n in rs.nodes
        ]
