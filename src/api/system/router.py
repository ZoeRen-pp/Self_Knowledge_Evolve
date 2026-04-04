"""System monitoring API — stats, history, drilldown."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.app_factory import get_app
from src.stats.drilldown import drilldown as _drilldown, METRIC_TO_QUERY
from src.api.system import review as _review

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/system", tags=["system"])

# ── Lazy singleton for scheduler + collector ─────────────────────────────────

_scheduler = None


def _get_scheduler():
    global _scheduler
    if _scheduler is None:
        app = get_app()
        from src.stats.collector import StatsCollector
        from src.stats.scheduler import StatsScheduler
        collector = StatsCollector(
            store=app.store,
            graph=app.graph,
            crawler_store=app.crawler_store,
        )
        _scheduler = StatsScheduler(collector, store=app.store)
        _scheduler.start()
    return _scheduler


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.get("/stats")
def get_stats(_app=Depends(get_app)):
    """Return the latest stats snapshot. Triggers immediate collection if none exists."""
    log.debug("GET /stats")
    store = _app.store

    # Try to read most recent snapshot
    row = store.fetchone(
        "SELECT snapshot, created_at FROM system_stats_snapshots ORDER BY created_at DESC LIMIT 1"
    )
    if row and row.get("snapshot"):
        return {"snapshot": row["snapshot"], "collected_at": str(row["created_at"])}

    # No snapshot yet — collect now
    scheduler = _get_scheduler()
    snapshot = scheduler.collect_now()
    return {"snapshot": snapshot, "collected_at": snapshot.get("timestamp")}


@router.get("/stats/history")
def get_stats_history(
    hours: int = Query(24, ge=1, le=168, description="Hours of history (max 7 days)"),
    _app=Depends(get_app),
):
    """Return historical snapshots for trend charts."""
    log.debug("GET /stats/history hours=%d", hours)
    rows = _app.store.fetchall(
        """SELECT snapshot, created_at FROM system_stats_snapshots
           WHERE created_at > NOW() - INTERVAL '%s hours'
           ORDER BY created_at ASC""",
        (hours,),
    )
    return {
        "hours": hours,
        "count": len(rows),
        "snapshots": [{"snapshot": r["snapshot"], "collected_at": str(r["created_at"])} for r in rows],
    }


@router.get("/drilldown/{metric_name}")
def drilldown_metric(
    metric_name: str,
    limit: int = Query(20, ge=1, le=200),
    threshold: int = Query(50, description="For super_nodes threshold"),
    days: int = Query(90, description="For stale_knowledge"),
    _app=Depends(get_app),
):
    """Drill down from an anomalous metric to specific knowledge items."""
    log.debug("GET /drilldown/%s limit=%d", metric_name, limit)
    data = _drilldown(metric_name, _app, limit=limit, threshold=threshold, days=days)
    return {"metric": metric_name, "result": data}


@router.get("/drilldown")
def list_drilldown_metrics():
    """List all available drilldown metrics."""
    return {"metrics": sorted(METRIC_TO_QUERY.keys())}


# ── Review endpoints ─────────────────────────────────────────────────────────

class ApproveRequest(BaseModel):
    reviewer: str
    note: str = ""
    parent_node_id: str | None = None
    aliases: list[str] | None = None

class RejectRequest(BaseModel):
    reviewer: str
    note: str = ""


@router.get("/review")
def list_review(
    type: str = Query("all", description="concept|relation|all"),
    status: str = Query("pending_review", description="pending_review|discovered|all"),
    limit: int = Query(20),
    _app=Depends(get_app),
):
    """List candidates for review."""
    return _review.list_candidates(type, status, limit, store=_app.store)


@router.get("/review/{candidate_id}")
def get_review(candidate_id: str, _app=Depends(get_app)):
    """Get single candidate details."""
    return _review.get_candidate(candidate_id, store=_app.store)


@router.post("/review/{candidate_id}/approve")
def approve(candidate_id: str, body: ApproveRequest, _app=Depends(get_app)):
    """Approve a candidate — writes to ontology, triggers backfill if concept."""
    result = _review.approve_candidate(
        candidate_id,
        reviewer=body.reviewer,
        note=body.note,
        parent_node_id=body.parent_node_id,
        aliases=body.aliases,
        store=_app.store,
        graph=_app.graph,
        ontology=_app.ontology,
    )

    # If concept approved, trigger background backfill
    if result.get("needs_backfill") and result.get("backfill_terms"):
        from src.stats.backfill import BackfillWorker
        worker = BackfillWorker(_app)
        worker.backfill_concept(result["node_id"], result["backfill_terms"])
        result["backfill"] = "started in background"

    return result


@router.post("/review/{candidate_id}/reject")
def reject(candidate_id: str, body: RejectRequest, _app=Depends(get_app)):
    """Reject a candidate."""
    return _review.reject_candidate(
        candidate_id, reviewer=body.reviewer, note=body.note, store=_app.store,
    )