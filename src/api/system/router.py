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



@router.get("/showcase/{case_id}")
def showcase(case_id: str, _app=Depends(get_app)):
    """Showcase cases powered by the declarative query engine."""
    log.info("GET /showcase/%s", case_id)
    from src.query.engine import QueryEngine
    engine = QueryEngine(_app)
    meta = _SHOWCASE_META.get(case_id)
    if not meta:
        return {"error": f"Unknown case: {case_id}"}
    try:
        builder = _SHOWCASE_BUILDERS[case_id]
        return builder(engine, _app, meta)
    except Exception as exc:
        log.error("Showcase %s error: %s", case_id, exc, exc_info=True)
        return {"error": str(exc)}


_SHOWCASE_META = {
    "fault_impact": {
        "case": "fault_impact", "title": "配置依赖链分析",
        "question": "新建 OSPF 网络，OSPF Instance 依赖哪些前置配置？配完 OSPF 后还需要配什么上层对象？",
        "why_unique": "传统搜索只能找到包含 OSPF 的文档。本系统从 OSPF Instance 出发，通过图遍历找到所有前置依赖和后置依赖，呈现完整的配置顺序链。",
    },
    "multi_source": {
        "case": "multi_source", "title": "多源矛盾裁决",
        "question": "不同文档对同一事实说法不一致，系统如何量化裁决？",
        "why_unique": "传统系统只返回文档列表。本系统自动检测矛盾，按来源权威等级和置信度评分量化裁决，并展示双方原文证据。",
    },
    "dependency_closure": {
        "case": "dependency_closure", "title": "动网割接影响面",
        "question": "割接 BGP Instance 时，哪些 Peer、Address Family、Route Policy 会被波及？",
        "why_unique": "传统搜索只能找提到 BGP 的文档。本系统通过传递闭包找到所有直接和间接依赖的可配置对象，让交付工程师完整评估影响面。",
    },
    "cross_layer_reasoning": {
        "case": "cross_layer_reasoning", "title": "方案设计依据追溯",
        "question": "为什么在这个场景下选择这个配置对象？",
        "why_unique": "五层模型能为方案设计提供完整的决策依据链，每一层都有原文证据支撑。",
    },
    "knowledge_gap": {
        "case": "knowledge_gap", "title": "知识盲区发现",
        "question": "哪些技术领域的知识储备不足？",
        "why_unique": "通过对比本体定义和实际知识覆盖，精确定位哪些概念缺少知识、哪些关系类型从未被使用。",
    },
}


def _nodes_to_list(result, var):
    arr = result.get(var)
    if isinstance(arr, list):
        out = []
        for n in arr:
            item = {"node_id": n.get("node_id", "")}
            item.update(n.get("properties") or n)
            out.append(item)
        return out
    return []


def _build_fault_impact(engine, app, meta):
    target = "IP.OSPF_INSTANCE"
    plan = {"intent": "OSPF config dependency", "steps": [
        {"op": "seed", "by": "id", "target": "node", "value": [target], "as": "$root"},
        {"op": "expand", "from": "$root", "any_of": ["depends_on", "configured_by", "contains", "explains", "composed_of"], "direction": "both", "depth": 2, "as": "$affected"},
        {"op": "expand", "from": "$root", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
        {"op": "aggregate", "function": "rerank", "from": "$segs", "query": "OSPF configuration dependency prerequisite neighbor adjacency", "limit": 8, "as": "$ranked"},
    ]}
    d = engine.execute(plan)
    r = d.get("result", {})
    affected = _nodes_to_list(r, "$affected")
    for n in affected:
        n.setdefault("name", n.get("canonical_name", ""))
        n.setdefault("layer", n.get("knowledge_layer", ""))
        n.setdefault("relation", n.get("_rel_type", ""))
        n.setdefault("predicate", "")
    segments = _nodes_to_list(r, "$ranked")
    facts = [dict(f) for f in app.store.fetchall(
        "SELECT subject, predicate, object, confidence FROM facts "
        "WHERE lifecycle_state='active' AND (subject LIKE 'IP.OSPF%%' OR object LIKE 'IP.OSPF%%') "
        "ORDER BY confidence DESC LIMIT 15"
    )]
    return {**meta, "affected_nodes": affected, "related_facts": facts,
            "source_segments": [{"raw_text": s.get("raw_text", ""), "segment_type": s.get("segment_type", ""), "section_title": s.get("section_title", "")} for s in segments]}


def _build_multi_source(engine, app, meta):
    store = app.store
    conflicts = store.fetchall(
        """SELECT cr.conflict_id, cr.conflict_type, cr.resolution,
                  cr.fact_id_a AS fid1, cr.fact_id_b AS fid2,
                  f1.subject, f1.predicate,
                  f1.object AS object_a, f2.object AS object_b,
                  f1.confidence AS conf_a, f2.confidence AS conf_b,
                  f1.lifecycle_state AS state_a, f2.lifecycle_state AS state_b
           FROM governance.conflict_records cr
           JOIN facts f1 ON cr.fact_id_a = f1.fact_id
           JOIN facts f2 ON cr.fact_id_b = f2.fact_id
           ORDER BY cr.created_at DESC LIMIT 10"""
    )
    detailed = []
    for c in conflicts[:5]:
        fact_ids = [str(c["fid1"]), str(c["fid2"])]
        plan = {"intent": "conflict evidence", "steps": [
            {"op": "seed", "by": "id", "target": "fact", "value": fact_ids, "as": "$facts"},
            {"op": "expand", "from": "$facts", "any_of": ["evidenced_by"], "direction": "outbound", "as": "$ev"},
            {"op": "project", "from": "$ev", "fields": ["node_id", "raw_text", "evidence_score", "source_doc_id"], "as": "$out"},
        ]}
        d = engine.execute(plan)
        ev_segs = _nodes_to_list(d.get("result", {}), "$out")
        ev_list = [{"text_preview": s.get("raw_text", "")[:300], "source_rank": "", "extraction_method": "", "doc_title": ""} for s in ev_segs]
        half = max(1, len(ev_list) // 2)
        conf_a, conf_b = float(c["conf_a"] or 0), float(c["conf_b"] or 0)
        if c.get("state_a") == "active" and c.get("state_b") != "active":
            verdict = "A"
        elif c.get("state_b") == "active" and c.get("state_a") != "active":
            verdict = "B"
        else:
            verdict = "A" if conf_a >= conf_b else "B"
        detailed.append({
            "subject": c["subject"], "predicate": c["predicate"],
            "conflict_type": c.get("conflict_type", "unknown"), "resolution": c.get("resolution", "open"),
            "claim_a": {"object": c["object_a"], "confidence": conf_a, "lifecycle_state": c.get("state_a", "unknown"), "evidence": ev_list[:half]},
            "claim_b": {"object": c["object_b"], "confidence": conf_b, "lifecycle_state": c.get("state_b", "unknown"), "evidence": ev_list[half:]},
            "verdict": verdict,
        })
    return {**meta, "conflicts": detailed, "total_conflicts": len(conflicts)}


def _build_dependency_closure(engine, app, meta):
    target = "IP.BGP_INSTANCE"
    plan = {"intent": "BGP cutover impact", "steps": [
        {"op": "seed", "by": "id", "target": "node", "value": [target], "as": "$root"},
        {"op": "expand", "from": "$root", "any_of": ["depends_on", "requires"], "direction": "outbound", "depth": 3, "track_path": True, "as": "$deps"},
        {"op": "expand", "from": "$root", "any_of": ["depends_on", "requires"], "direction": "inbound", "depth": 3, "track_path": True, "as": "$dependents"},
        {"op": "expand", "from": "$root", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
        {"op": "aggregate", "function": "rerank", "from": "$segs", "query": "BGP dependency prerequisite cutover impact", "limit": 5, "as": "$ranked"},
    ]}
    d = engine.execute(plan)
    r = d.get("result", {})
    root_node = _nodes_to_list(r, "$root")
    name = root_node[0].get("canonical_name", target) if root_node else target
    segments = _nodes_to_list(r, "$ranked")
    facts = [dict(f) for f in app.store.fetchall(
        "SELECT subject, predicate, object, confidence FROM facts "
        "WHERE lifecycle_state='active' AND (subject LIKE 'IP.BGP%%' OR object LIKE 'IP.BGP%%') "
        "ORDER BY confidence DESC LIMIT 15"
    )]
    return {**meta, "target": {"node_id": target, "name": name},
            "related_facts": facts,
            "source_segments": [{"raw_text": s.get("raw_text", "")[:400], "section_title": s.get("section_title", ""), "segment_type": s.get("segment_type", "")} for s in segments]}


def _build_cross_layer(engine, app, meta):
    graph = app.graph
    chains = graph.read(
        """MATCH (c:OntologyNode)-[r1]-(m:MechanismNode),
                 (m)-[r2]-(mt:MethodNode)
           WHERE c.lifecycle_state = 'active'
           OPTIONAL MATCH (mt)-[r3]-(cn:ConditionRuleNode)
           OPTIONAL MATCH (cn)-[r4]-(s:ScenarioPatternNode)
           RETURN c.node_id AS concept_id, c.canonical_name AS concept,
                  m.canonical_name AS mechanism, m.description AS mech_desc,
                  mt.canonical_name AS method, mt.description AS method_desc,
                  cn.canonical_name AS condition, cn.description AS cond_desc,
                  s.canonical_name AS scenario, s.description AS scenario_desc,
                  type(r1) AS rel1, type(r2) AS rel2, type(r3) AS rel3, type(r4) AS rel4
           LIMIT 10"""
    )
    enriched = []
    for chain in chains:
        concept_id = chain["concept_id"]
        plan = {"steps": [
            {"op": "seed", "by": "id", "target": "node", "value": [concept_id], "as": "$n"},
            {"op": "expand", "from": "$n", "any_of": ["tagged_in"], "direction": "outbound", "as": "$segs"},
            {"op": "aggregate", "function": "rank", "from": "$segs", "by_fields": ["token_count"], "order": "desc", "limit": 2, "as": "$top"},
            {"op": "project", "from": "$top", "fields": ["raw_text", "section_title"], "as": "$out"},
        ]}
        d = engine.execute(plan)
        segs = _nodes_to_list(d.get("result", {}), "$out")
        enriched.append({**dict(chain), "source_segments": [{"raw_text": s.get("raw_text", "")[:300], "section_title": s.get("section_title", "")} for s in segs]})
    return {**meta, "chains": enriched}


def _build_knowledge_gap(engine, app, meta):
    store, graph = app.store, app.graph
    from src.ontology.registry import OntologyRegistry
    reg = OntologyRegistry.from_default()
    total_nodes_row = graph.read("MATCH (n:OntologyNode) WHERE n.lifecycle_state='active' RETURN count(n) AS cnt")
    total_nodes = total_nodes_row[0]["cnt"] if total_nodes_row else 0
    tagged_row = store.fetchone("SELECT count(DISTINCT ontology_node_id) AS cnt FROM segment_tags WHERE tag_type='canonical'")
    tagged = tagged_row["cnt"] if tagged_row else 0
    core_ids = ['IP.BGP_INSTANCE','IP.BGP_PEER','IP.BGP_ADDRESS_FAMILY','IP.OSPF_INSTANCE','IP.OSPF_AREA','IP.OSPF_INTERFACE',
                'IP.ISIS_INSTANCE','IP.MPLS_GLOBAL','IP.MPLS_SR','IP.EVPN_INSTANCE','IP.VXLAN_VNI','IP.SRV6_LOCATOR',
                'IP.BFD_SESSION','IP.VRRP_GROUP','IP.QOS','IP.NAT_RULE','IP.DHCP_RELAY','IP.INTERFACE','IP.ROUTE_POLICY','IP.ROUTE_TABLE']
    tagged_ids = {r["ontology_node_id"] for r in store.fetchall(
        "SELECT DISTINCT ontology_node_id FROM segment_tags WHERE tag_type='canonical'"
    )}
    no_segs = [{"node_id": nid} for nid in core_ids if nid not in tagged_ids]
    used_preds = {r["predicate"] for r in store.fetchall("SELECT DISTINCT predicate FROM facts WHERE lifecycle_state='active'")}
    unused = sorted(reg.relation_ids - used_preds)
    no_facts = graph.read(
        """MATCH (n:OntologyNode) WHERE n.lifecycle_state = 'active'
           OPTIONAL MATCH (n)<-[:TAGGED_IN]-(s)
           WITH n, count(s) AS seg_count WHERE seg_count = 0
           RETURN n.node_id AS node_id, n.canonical_name AS name
           ORDER BY n.node_id LIMIT 20"""
    )
    return {**meta,
            "coverage": {"total_ontology_nodes": total_nodes, "nodes_with_knowledge": tagged, "coverage_rate": round(tagged / max(total_nodes, 1), 4)},
            "nodes_without_facts": [dict(r) for r in no_facts],
            "core_nodes_without_segments": no_segs,
            "unused_relation_types": unused[:20], "unused_relation_count": len(unused)}


_SHOWCASE_BUILDERS = {
    "fault_impact": _build_fault_impact,
    "multi_source": _build_multi_source,
    "dependency_closure": _build_dependency_closure,
    "cross_layer_reasoning": _build_cross_layer,
    "knowledge_gap": _build_knowledge_gap,
}

# ── Pipeline flow + recent activity ─────────────────────────────────────────

@router.get("/pipeline_flow")
def pipeline_flow(_app=Depends(get_app)):
    """Pipeline stage counts for the flow diagram."""
    s = _app.store
    cs = _app.crawler_store if hasattr(_app, 'crawler_store') and _app.crawler_store else s
    docs_total = (s.fetchone("SELECT count(*) AS c FROM documents") or {}).get("c", 0)
    docs_indexed = (s.fetchone("SELECT count(*) AS c FROM documents WHERE status='indexed'") or {}).get("c", 0)
    segs = (s.fetchone("SELECT count(*) AS c FROM segments WHERE lifecycle_state='active'") or {}).get("c", 0)
    tags = (s.fetchone("SELECT count(*) AS c FROM segment_tags") or {}).get("c", 0)
    facts = (s.fetchone("SELECT count(*) AS c FROM facts WHERE lifecycle_state='active'") or {}).get("c", 0)
    evidence = (s.fetchone("SELECT count(*) AS c FROM evidence") or {}).get("c", 0)
    candidates = (s.fetchone("SELECT count(*) AS c FROM governance.evolution_candidates") or {}).get("c", 0)
    try:
        crawl_done = (cs.fetchone("SELECT count(*) AS c FROM crawl_tasks WHERE status='done'") or {}).get("c", 0)
    except Exception:
        crawl_done = 0
    neo4j_nodes = 0
    neo4j_rels = 0
    try:
        g = _app.graph
        r = g.read("MATCH (n) RETURN count(n) AS c")
        neo4j_nodes = r[0]["c"] if r else 0
        r2 = g.read("MATCH ()-[r]->() RETURN count(r) AS c")
        neo4j_rels = r2[0]["c"] if r2 else 0
    except Exception:
        pass
    return {
        "crawled": crawl_done,
        "documents": docs_total,
        "indexed": docs_indexed,
        "segments": segs,
        "tags": tags,
        "facts": facts,
        "evidence": evidence,
        "candidates": candidates,
        "neo4j_nodes": neo4j_nodes,
        "neo4j_relationships": neo4j_rels,
    }


@router.get("/recent_activity")
def recent_activity(
    limit: int = Query(20, ge=1, le=100),
    _app=Depends(get_app),
):
    """Recent review activity + recently processed documents."""
    s = _app.store
    reviews = s.fetchall(
        """SELECT review_id, object_type, action, reviewer, note,
                  created_at AT TIME ZONE 'UTC' AS created_at
           FROM governance.review_records
           ORDER BY created_at DESC LIMIT %s""",
        (limit,),
    )
    recent_docs = s.fetchall(
        """SELECT source_doc_id, title, status, doc_type,
                  created_at AT TIME ZONE 'UTC' AS created_at
           FROM documents ORDER BY created_at DESC LIMIT %s""",
        (limit,),
    )
    return {
        "reviews": [dict(r) for r in reviews],
        "documents": [dict(r) for r in recent_docs],
    }


@router.get("/candidate_distribution")
def candidate_distribution(_app=Depends(get_app)):
    """Candidate source_count distribution for histogram."""
    s = _app.store
    rows = s.fetchall(
        """SELECT
             CASE
               WHEN source_count = 1 THEN '1'
               WHEN source_count = 2 THEN '2'
               WHEN source_count BETWEEN 3 AND 5 THEN '3-5'
               WHEN source_count BETWEEN 6 AND 10 THEN '6-10'
               WHEN source_count BETWEEN 11 AND 20 THEN '11-20'
               ELSE '20+'
             END AS bucket,
             count(*) AS cnt
           FROM governance.evolution_candidates
           WHERE review_status NOT IN ('rejected', 'auto_rejected')
           GROUP BY bucket
           ORDER BY min(source_count)"""
    )
    status_rows = s.fetchall(
        """SELECT review_status, count(*) AS cnt
           FROM governance.evolution_candidates
           GROUP BY review_status"""
    )
    type_rows = s.fetchall(
        """SELECT candidate_type, count(*) AS cnt
           FROM governance.evolution_candidates
           WHERE review_status NOT IN ('rejected', 'auto_rejected')
           GROUP BY candidate_type"""
    )
    return {
        "distribution": [dict(r) for r in rows],
        "by_status": {r["review_status"]: r["cnt"] for r in status_rows},
        "by_type": {r["candidate_type"]: r["cnt"] for r in type_rows},
    }


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
    limit: int = Query(100),
    _app=Depends(get_app),
):
    """List candidates for review."""
    return _review.list_candidates(type, status, limit, store=_app.store)


@router.get("/review/{candidate_id}")
def get_review(candidate_id: str, _app=Depends(get_app)):
    """Get single candidate details + related segments from recorded examples."""
    candidate = _review.get_candidate(candidate_id, store=_app.store)
    if candidate.get("error"):
        return candidate

    # Extract segment_ids from examples (recorded during Pipeline Stage 3/4)
    import json
    examples = candidate.get("examples") or []
    if isinstance(examples, str):
        try:
            examples = json.loads(examples)
        except Exception:
            examples = []

    segment_ids = list({ex.get("segment_id") for ex in examples if ex.get("segment_id")})

    # Retrieve full segment texts via filter operator
    segments = []
    if segment_ids:
        for sid in segment_ids[:10]:  # limit to 10 segments
            result = _app.query("filter", object_type="segment",
                                filters={"segment_id": sid}, page_size=1).data
            items = result.get("items") or []
            if items:
                segments.append(items[0])

    candidate["related_segments"] = segments
    candidate["segment_count"] = len(segment_ids)
    return candidate


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


class MergeRequest(BaseModel):
    candidate_ids: list[str]
    primary_id: str | None = None

class CheckSynonymsRequest(BaseModel):
    candidate_ids: list[str]


@router.post("/review/merge")
def merge(body: MergeRequest, _app=Depends(get_app)):
    """Merge multiple candidates into one."""
    return _review.merge_candidates(
        body.candidate_ids, primary_id=body.primary_id, store=_app.store,
    )


@router.post("/review/check_synonyms")
def check_synonyms(body: CheckSynonymsRequest, _app=Depends(get_app)):
    """Ask LLM if candidates are synonyms."""
    return _review.check_synonyms(body.candidate_ids, store=_app.store)