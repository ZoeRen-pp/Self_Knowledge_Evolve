"""FastAPI router — delegates all operator calls to OperatorRegistry via SemanticApp."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from semcore.operators.base import OperatorResult
from src.app_factory import get_app

router = APIRouter(prefix="/api/v1/semantic", tags=["semantic"])


def _wrap(result: OperatorResult) -> dict:
    return {
        "meta": {
            "ontology_version": result.ontology_version,
            "latency_ms":       result.latency_ms,
        },
        "result": result.data,
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

class SemanticSearchRequest(BaseModel):
    query:          str
    top_k:          int   = 5
    min_similarity: float = 0.5
    layer_filter:   str | None = None   # concept / mechanism / method / condition / scenario

class EduSearchRequest(BaseModel):
    query:          str
    top_k:          int   = 5
    min_similarity: float = 0.5
    title_weight:   float = 0.3   # weight for title_vec similarity (content_weight = 1 - title_weight)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/lookup")
def lookup(
    term:             str   = Query(..., description="Term or alias to look up"),
    scope:            str | None = Query(None),
    lang:             str   = Query("en"),
    ontology_version: str | None = Query(None),
    include_evidence: bool  = Query(False),
    max_evidence:     int   = Query(3),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "lookup",
            term=term, scope=scope, lang=lang,
            ontology_version=ontology_version,
            include_evidence=include_evidence,
            max_evidence=max_evidence,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/resolve")
def resolve(
    alias:  str = Query(...),
    scope:  str | None = Query(None),
    vendor: str | None = Query(None),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query("resolve", alias=alias, scope=scope, vendor=vendor))
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
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "expand",
            node_id=node_id,
            relation_types=relation_types or None,
            depth=depth,
            min_confidence=min_confidence,
            include_facts=include_facts,
            include_segments=include_segments,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.post("/filter")
def filter_objects(body: FilterRequest, _app = Depends(get_app)):
    try:
        return _wrap(_app.query(
            "filter",
            object_type=body.object_type,
            filters=body.filters,
            sort_by=body.sort_by,
            sort_order=body.sort_order,
            page=body.page,
            page_size=body.page_size,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/path")
def path_infer(
    start_node_id:   str   = Query(...),
    end_node_id:     str   = Query(...),
    relation_policy: str   = Query("all"),
    max_hops:        int   = Query(5, ge=1, le=8),
    min_confidence:  float = Query(0.5),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "path",
            start_node_id=start_node_id,
            end_node_id=end_node_id,
            relation_policy=relation_policy,
            max_hops=max_hops,
            min_confidence=min_confidence,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/dependency_closure")
def dependency_closure(
    node_id:          str        = Query(...),
    relation_types:   list[str]  = Query(default=[]),
    max_depth:        int        = Query(6, ge=1, le=10),
    include_optional: bool       = Query(False),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "dependency_closure",
            node_id=node_id,
            relation_types=relation_types or None,
            max_depth=max_depth,
            include_optional=include_optional,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.post("/impact_propagate")
def impact_propagate(body: ImpactRequest, _app = Depends(get_app)):
    try:
        return _wrap(_app.query(
            "impact_propagate",
            event_node_id=body.event_node_id,
            event_type=body.event_type,
            relation_policy=body.relation_policy,
            max_depth=body.max_depth,
            min_confidence=body.min_confidence,
            context=body.context,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/evidence_rank")
def evidence_rank(
    fact_id:     str = Query(...),
    rank_by:     str = Query("evidence_score"),
    max_results: int = Query(10),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "evidence_rank",
            fact_id=fact_id,
            rank_by=rank_by,
            max_results=max_results,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/conflict_detect")
def conflict_detect(
    topic_node_id:  str         = Query(...),
    predicate:      str | None  = Query(None),
    min_confidence: float       = Query(0.5),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "conflict_detect",
            topic_node_id=topic_node_id,
            predicate=predicate,
            min_confidence=min_confidence,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.post("/fact_merge")
def fact_merge(body: FactMergeRequest, _app = Depends(get_app)):
    try:
        return _wrap(_app.query(
            "fact_merge",
            fact_ids=body.fact_ids,
            merge_strategy=body.merge_strategy,
            canonical_fact=body.canonical_fact,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/candidate_discover")
def candidate_discover(
    window_days:      int        = Query(...),
    min_frequency:    int        = Query(5),
    domain:           str | None = Query(None),
    min_source_count: int        = Query(2),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "candidate_discover",
            window_days=window_days,
            min_frequency=min_frequency,
            domain=domain,
            min_source_count=min_source_count,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.get("/attach_score")
def attach_score(
    candidate_id:         str       = Query(...),
    candidate_parent_ids: list[str] = Query(default=[]),
    _app = Depends(get_app),
):
    try:
        return _wrap(_app.query(
            "attach_score",
            candidate_id=candidate_id,
            candidate_parent_ids=candidate_parent_ids or None,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.post("/evolution_gate")
def evolution_gate(body: EvolutionGateRequest, _app = Depends(get_app)):
    try:
        return _wrap(_app.query("evolution_gate", candidate_id=body.candidate_id))
    except Exception as exc:
        return _err(str(exc))


@router.post("/semantic_search")
def semantic_search(body: SemanticSearchRequest, _app = Depends(get_app)):
    """
    Semantic similarity search over knowledge segments using pgvector.

    Requires EMBEDDING_ENABLED=true and embeddings already written by stage6.
    Falls back to empty result if embedding is not available.
    """
    try:
        return _wrap(_app.query(
            "semantic_search",
            query=body.query,
            top_k=body.top_k,
            min_similarity=body.min_similarity,
            layer_filter=body.layer_filter,
        ))
    except Exception as exc:
        return _err(str(exc))


@router.post("/edu_search")
def edu_search(body: EduSearchRequest, _app = Depends(get_app)):
    """
    Dual-vector semantic search over segments using title_vec + content_vec.

    Combines title and content similarity with configurable weighting.
    Requires EMBEDDING_ENABLED=true and embeddings written by stage6.
    """
    try:
        return _wrap(_app.query(
            "edu_search",
            query=body.query,
            top_k=body.top_k,
            min_similarity=body.min_similarity,
            title_weight=body.title_weight,
        ))
    except Exception as exc:
        return _err(str(exc))