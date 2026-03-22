"""FastAPI router wiring all semantic operators to HTTP endpoints."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query, Body
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.config.settings import settings
from src.api.semantic import (
    lookup as _lookup,
    resolve as _resolve,
    expand as _expand,
    filter as _filter,
    path as _path,
    dependency as _dep,
    impact as _impact,
    evidence as _evidence,
    evolution as _evo,
)

router = APIRouter(prefix="/api/v1/semantic", tags=["semantic"])


def _wrap(data: Any, t0: float) -> dict:
    """Attach metadata envelope to every response."""
    return {
        "meta": {
            "ontology_version": settings.ONTOLOGY_VERSION,
            "latency_ms":       round((time.monotonic() - t0) * 1000),
        },
        "result": data,
    }


def _err(msg: str) -> JSONResponse:
    return JSONResponse(status_code=400, content={"error": msg})


# ── Request / Response models ─────────────────────────────────────────────────

class FilterRequest(BaseModel):
    object_type: str
    filters:     dict = {}
    sort_by:     str = "confidence"
    sort_order:  str = "desc"
    page:        int = 1
    page_size:   int = 20

class ImpactRequest(BaseModel):
    event_node_id:   str
    event_type:      str = "fault"
    relation_policy: str = "causal"
    max_depth:       int = 4
    min_confidence:  float = 0.6
    context:         dict = {}

class FactMergeRequest(BaseModel):
    fact_ids:        list[str]
    merge_strategy:  str = "highest_confidence"
    canonical_fact:  dict | None = None

class EvolutionGateRequest(BaseModel):
    candidate_id: str


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/lookup")
def lookup(
    term:             str   = Query(..., description="Term or alias to look up"),
    scope:            str | None = Query(None),
    lang:             str   = Query("en"),
    ontology_version: str | None = Query(None),
    include_evidence: bool  = Query(False),
    max_evidence:     int   = Query(3),
):
    t0 = time.monotonic()
    try:
        return _wrap(_lookup.lookup(term, scope, lang, ontology_version, include_evidence, max_evidence), t0)
    except Exception as exc:
        return _err(str(exc))


@router.get("/resolve")
def resolve(
    alias:  str = Query(...),
    scope:  str | None = Query(None),
    vendor: str | None = Query(None),
):
    t0 = time.monotonic()
    try:
        return _wrap(_resolve.resolve(alias, scope, vendor), t0)
    except Exception as exc:
        return _err(str(exc))


@router.get("/expand")
def expand(
    node_id:          str        = Query(...),
    relation_types:   list[str]  = Query(default=[]),
    depth:            int        = Query(1, ge=1, le=3),
    min_confidence:   float      = Query(0.5),
    include_facts:    bool       = Query(True),
    include_segments: bool       = Query(False),
):
    t0 = time.monotonic()
    try:
        return _wrap(
            _expand.expand(node_id, relation_types or None, depth, min_confidence, include_facts, include_segments),
            t0,
        )
    except Exception as exc:
        return _err(str(exc))


@router.post("/filter")
def filter_objects(body: FilterRequest):
    t0 = time.monotonic()
    try:
        return _wrap(
            _filter.filter_objects(body.object_type, body.filters, body.sort_by, body.sort_order, body.page, body.page_size),
            t0,
        )
    except Exception as exc:
        return _err(str(exc))


@router.get("/path")
def path_infer(
    start_node_id:   str   = Query(...),
    end_node_id:     str   = Query(...),
    relation_policy: str   = Query("all"),
    max_hops:        int   = Query(5, ge=1, le=8),
    min_confidence:  float = Query(0.5),
):
    t0 = time.monotonic()
    try:
        return _wrap(_path.path_infer(start_node_id, end_node_id, relation_policy, max_hops, min_confidence), t0)
    except Exception as exc:
        return _err(str(exc))


@router.get("/dependency_closure")
def dependency_closure(
    node_id:          str        = Query(...),
    relation_types:   list[str]  = Query(default=[]),
    max_depth:        int        = Query(6, ge=1, le=10),
    include_optional: bool       = Query(False),
):
    t0 = time.monotonic()
    try:
        return _wrap(
            _dep.dependency_closure(node_id, relation_types or None, max_depth, include_optional),
            t0,
        )
    except Exception as exc:
        return _err(str(exc))


@router.post("/impact_propagate")
def impact_propagate(body: ImpactRequest):
    t0 = time.monotonic()
    try:
        return _wrap(
            _impact.impact_propagate(
                body.event_node_id, body.event_type, body.relation_policy,
                body.max_depth, body.min_confidence, body.context,
            ),
            t0,
        )
    except Exception as exc:
        return _err(str(exc))


@router.get("/evidence_rank")
def evidence_rank(
    fact_id:     str = Query(...),
    rank_by:     str = Query("evidence_score"),
    max_results: int = Query(10),
):
    t0 = time.monotonic()
    try:
        return _wrap(_evidence.evidence_rank(fact_id, rank_by, max_results), t0)
    except Exception as exc:
        return _err(str(exc))


@router.get("/conflict_detect")
def conflict_detect(
    topic_node_id:  str         = Query(...),
    predicate:      str | None  = Query(None),
    min_confidence: float       = Query(0.5),
):
    t0 = time.monotonic()
    try:
        return _wrap(_evidence.conflict_detect(topic_node_id, predicate, min_confidence), t0)
    except Exception as exc:
        return _err(str(exc))


@router.post("/fact_merge")
def fact_merge(body: FactMergeRequest):
    t0 = time.monotonic()
    try:
        return _wrap(_evidence.fact_merge(body.fact_ids, body.merge_strategy, body.canonical_fact), t0)
    except Exception as exc:
        return _err(str(exc))


@router.get("/candidate_discover")
def candidate_discover(
    window_days:      int        = Query(...),
    min_frequency:    int        = Query(5),
    domain:           str | None = Query(None),
    min_source_count: int        = Query(2),
):
    t0 = time.monotonic()
    try:
        return _wrap(_evo.candidate_discover(window_days, min_frequency, domain, min_source_count), t0)
    except Exception as exc:
        return _err(str(exc))


@router.get("/attach_score")
def attach_score(
    candidate_id:         str       = Query(...),
    candidate_parent_ids: list[str] = Query(default=[]),
):
    t0 = time.monotonic()
    try:
        return _wrap(_evo.attach_score(candidate_id, candidate_parent_ids or None), t0)
    except Exception as exc:
        return _err(str(exc))


@router.post("/evolution_gate")
def evolution_gate(body: EvolutionGateRequest):
    t0 = time.monotonic()
    try:
        return _wrap(_evo.evolution_gate(body.candidate_id), t0)
    except Exception as exc:
        return _err(str(exc))