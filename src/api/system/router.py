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
    """Showcase cases — complex graph+PG combined queries that demonstrate unique value."""
    log.info("GET /showcase/%s", case_id)
    store = _app.store
    graph = _app.graph
    try:
        if case_id == "fault_impact":
            return _case_fault_impact(store, graph)
        elif case_id == "multi_source":
            return _case_multi_source(store, graph)
        elif case_id == "dependency_closure":
            return _case_dependency_closure(store, graph)
        elif case_id == "cross_layer_reasoning":
            return _case_cross_layer(store, graph)
        elif case_id == "knowledge_gap":
            return _case_knowledge_gap(store, graph)
        else:
            return {"error": f"Unknown case: {case_id}"}
    except Exception as exc:
        log.error("Showcase %s error: %s", case_id, exc, exc_info=True)
        return {"error": str(exc)}


def _case_fault_impact(store, graph):
    """Case 1: 故障全链路推演 — OSPF Instance 故障，影响什么？怎么排查？"""
    target = "IP.OSPF_INSTANCE"
    # Graph: OSPF impact propagation — start from OSPF Instance, traverse all
    # direct neighbours and then walk DEPENDS_ON reverse to find affected objects
    affected = graph.read(
        """MATCH (n:OntologyNode {node_id: $target})-[r]-(m)
           WHERE m:OntologyNode OR m:MechanismNode OR m:ScenarioPatternNode
                 OR m:MethodNode OR m:ConditionRuleNode
           RETURN m.node_id AS node_id, m.canonical_name AS name,
                  labels(m)[0] AS layer, type(r) AS relation, r.predicate AS predicate
           ORDER BY labels(m)[0]""",
        target=target,
    )
    # Also find objects that transitively depend on OSPF
    upstream = graph.read(
        """MATCH path = (upstream:OntologyNode)-[:DEPENDS_ON*1..3]->(n:OntologyNode {node_id: $target})
           RETURN upstream.node_id AS node_id, upstream.canonical_name AS name,
                  'OntologyNode' AS layer, 'DEPENDS_ON' AS relation,
                  'depends_on (transitive)' AS predicate
           ORDER BY length(path)""",
        target=target,
    )
    all_affected = [dict(r) for r in affected] + [dict(r) for r in upstream]
    # PG: related segments with troubleshooting content
    segments = store.fetchall(
        """SELECT s.raw_text, s.segment_type, s.section_title, st.tag_value
           FROM segments s
           JOIN segment_tags st ON s.segment_id = st.segment_id
           WHERE st.ontology_node_id LIKE 'IP.OSPF%%'
             AND (s.segment_type IN ('troubleshooting', 'fault', 'mechanism', 'definition')
                  OR s.raw_text ILIKE '%%neighbor%%' OR s.raw_text ILIKE '%%adjacen%%'
                  OR s.raw_text ILIKE '%%failure%%' OR s.raw_text ILIKE '%%down%%')
             AND s.lifecycle_state = 'active'
           ORDER BY s.token_count DESC
           LIMIT 8"""
    )
    # PG: related facts (match any OSPF-family node)
    facts = store.fetchall(
        """SELECT f.subject, f.predicate, f.object, f.confidence
           FROM facts f WHERE f.lifecycle_state = 'active'
             AND (f.subject LIKE 'IP.OSPF%%' OR f.object LIKE 'IP.OSPF%%')
           ORDER BY f.confidence DESC LIMIT 15"""
    )
    return {
        "case": "fault_impact",
        "title": "故障全链路推演",
        "question": "OSPF Instance 发生故障，哪些接口、路由策略、业务场景受影响？每个环节该怎么排查？",
        "why_unique": "传统搜索只能找到包含 OSPF 的文档。本系统从 OSPF Instance 出发，通过图遍历找到所有直接关联和传递依赖的对象（接口、Area、路由导入等），并从 PG 中检索对应的排障原文。",
        "affected_nodes": all_affected,
        "related_facts": [dict(r) for r in facts],
        "source_segments": [dict(r) for r in segments],
    }


def _case_multi_source(store, graph):
    """Case 2: 多源矛盾裁决 — 不同来源说法不一，听谁的？"""
    # Use governance.conflict_records for real conflict pairs
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
           ORDER BY cr.created_at DESC
           LIMIT 10"""
    )
    # For each conflict, get evidence with source authority
    detailed = []
    for c in conflicts[:5]:
        ev_a = store.fetchall(
            """SELECT e.source_rank, e.extraction_method,
                      left(s.raw_text, 300) AS text_preview,
                      d.title AS doc_title
               FROM evidence e
               JOIN segments s ON e.segment_id = s.segment_id
               LEFT JOIN documents d ON e.source_doc_id = d.source_doc_id
               WHERE e.fact_id = %s LIMIT 2""", (c["fid1"],)
        )
        ev_b = store.fetchall(
            """SELECT e.source_rank, e.extraction_method,
                      left(s.raw_text, 300) AS text_preview,
                      d.title AS doc_title
               FROM evidence e
               JOIN segments s ON e.segment_id = s.segment_id
               LEFT JOIN documents d ON e.source_doc_id = d.source_doc_id
               WHERE e.fact_id = %s LIMIT 2""", (c["fid2"],)
        )
        conf_a = float(c["conf_a"] or 0)
        conf_b = float(c["conf_b"] or 0)
        # Determine verdict: prefer active over conflicted, then higher confidence
        if c.get("state_a") == "active" and c.get("state_b") != "active":
            verdict = "A"
        elif c.get("state_b") == "active" and c.get("state_a") != "active":
            verdict = "B"
        else:
            verdict = "A" if conf_a >= conf_b else "B"
        detailed.append({
            "subject": c["subject"], "predicate": c["predicate"],
            "conflict_type": c.get("conflict_type", "unknown"),
            "resolution": c.get("resolution", "open"),
            "claim_a": {"object": c["object_a"], "confidence": conf_a,
                        "lifecycle_state": c.get("state_a", "unknown"),
                        "evidence": [dict(e) for e in ev_a]},
            "claim_b": {"object": c["object_b"], "confidence": conf_b,
                        "lifecycle_state": c.get("state_b", "unknown"),
                        "evidence": [dict(e) for e in ev_b]},
            "verdict": verdict,
        })
    return {
        "case": "multi_source",
        "title": "多源矛盾裁决",
        "question": "不同文档对同一事实说法不一致，系统如何量化裁决？",
        "why_unique": "传统系统只返回文档列表，人自己判断。本系统自动检测矛盾，按来源权威等级（S>A>B>C）和置信度评分量化���决，并展示双方原文证据。",
        "conflicts": detailed,
        "total_conflicts": len(conflicts),
    }


def _case_dependency_closure(store, graph):
    """Case 3: 变更影响面评估 — 改 BGP Instance 配置，全网波及多少东西？"""
    target = "IP.BGP_INSTANCE"
    # Graph: dependency closure (all transitive dependencies)
    deps = graph.read(
        """MATCH path = (n:OntologyNode {node_id: $target})
                  -[:DEPENDS_ON|USES_PROTOCOL|REQUIRES*1..3]->(m)
           RETURN m.node_id AS node_id, m.canonical_name AS name,
                  length(path) AS hops,
                  [r IN relationships(path) | type(r)] AS path_types
           ORDER BY hops""",
        target=target,
    )
    # Reverse: who depends on BGP Instance?
    dependents = graph.read(
        """MATCH path = (m)-[:DEPENDS_ON|USES_PROTOCOL|REQUIRES*1..3]->
                  (n:OntologyNode {node_id: $target})
           RETURN m.node_id AS node_id, m.canonical_name AS name,
                  labels(m)[0] AS layer, length(path) AS hops
           ORDER BY hops""",
        target=target,
    )
    # PG: segments about BGP dependencies (match any BGP-family node)
    segments = store.fetchall(
        """SELECT left(s.raw_text, 400) AS raw_text, s.section_title, s.segment_type
           FROM segments s
           JOIN segment_tags st ON s.segment_id = st.segment_id
           WHERE st.ontology_node_id LIKE 'IP.BGP%%'
             AND (s.raw_text ILIKE '%%depend%%' OR s.raw_text ILIKE '%%require%%'
                  OR s.raw_text ILIKE '%%prerequisite%%')
             AND s.lifecycle_state = 'active'
           LIMIT 5"""
    )
    target_node = graph.read(
        "MATCH (n:OntologyNode {node_id: $t}) RETURN n.canonical_name AS name", t=target
    )
    name = target_node[0]["name"] if target_node else target
    return {
        "case": "dependency_closure",
        "title": "变更影响面评估",
        "question": f"要修改 {name} 配置，哪些 Peer、Address Family、Route Policy 会被波及？",
        "why_unique": "传统搜索只能找提到 BGP 的文档。本系统通过图数据库的传递闭包，从 BGP Instance 出发找到所有直接和间接依赖的可配置对象，量化变更影响面。",
        "target": {"node_id": target, "name": name},
        "depends_on": [dict(r) for r in deps],
        "depended_by": [dict(r) for r in dependents],
        "source_segments": [dict(r) for r in segments],
    }


def _case_cross_layer(store, graph):
    """Case 4: 五层推理链还原 — 从可配置对象到场景的完整推理路径"""
    # Try full 5-layer chains first; fall back to partial chains if none found
    chains = graph.read(
        """MATCH (c:OntologyNode)-[r1]-(m:MechanismNode),
                 (m)-[r2]-(mt:MethodNode),
                 (mt)-[r3]-(cn:ConditionRuleNode),
                 (cn)-[r4]-(s:ScenarioPatternNode)
           WHERE c.lifecycle_state = 'active'
           RETURN c.node_id AS concept_id, c.canonical_name AS concept,
                  m.canonical_name AS mechanism, m.description AS mech_desc,
                  mt.canonical_name AS method, mt.description AS method_desc,
                  cn.canonical_name AS condition, cn.description AS cond_desc,
                  s.canonical_name AS scenario, s.description AS scenario_desc,
                  type(r1) AS rel1, type(r2) AS rel2, type(r3) AS rel3, type(r4) AS rel4
           LIMIT 10"""
    )
    if not chains:
        # Fall back to partial chains: concept → mechanism → method (3-layer)
        chains = graph.read(
            """MATCH (c:OntologyNode)-[r1]-(m:MechanismNode)-[r2]-(mt:MethodNode)
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
    # For each chain's concept, get source evidence
    enriched = []
    for chain in chains:
        segments = store.fetchall(
            """SELECT left(s.raw_text, 300) AS raw_text, s.section_title
               FROM segments s
               JOIN segment_tags st ON s.segment_id = st.segment_id
               WHERE st.ontology_node_id = %s AND s.lifecycle_state = 'active'
               ORDER BY s.token_count DESC LIMIT 2""",
            (chain["concept_id"],)
        )
        enriched.append({
            **dict(chain),
            "source_segments": [dict(s) for s in segments],
        })
    return {
        "case": "cross_layer_reasoning",
        "title": "五层推理链还原",
        "question": "为什么说某个可配置对象适合某个场景？把从配置对象到部署场景的推理过程完整展示出来。",
        "why_unique": "传统知识图谱只有概念和关系两层。本系统的五层模型（YANG可配置对象→协议机制→操作方法→适用条件→业务场景）能构建完整的推理路径，每一层都有原文证据支撑。",
        "chains": enriched,
    }


def _case_knowledge_gap(store, graph):
    """Case 5: 知识空白发现（元认知）— 系统知道自己不知道���么"""
    # Nodes with no facts
    no_facts = graph.read(
        """MATCH (n:OntologyNode) WHERE n.lifecycle_state = 'active'
           AND NOT (n)<-[:TAGGED_WITH]-(:KnowledgeSegment)-[:EXTRACTED_FROM]->(:Fact)
           RETURN n.node_id AS node_id, n.canonical_name AS name
           ORDER BY n.node_id LIMIT 20"""
    )
    # Nodes with no segments at all
    no_segments_row = store.fetchall(
        """SELECT n_id AS node_id FROM (
             SELECT DISTINCT st.ontology_node_id AS n_id FROM segment_tags st
             WHERE st.tag_type = 'canonical'
           ) tagged
           RIGHT JOIN (SELECT unnest(ARRAY[
             'IP.BGP_INSTANCE','IP.BGP_PEER','IP.BGP_ADDRESS_FAMILY',
             'IP.OSPF_INSTANCE','IP.OSPF_AREA','IP.OSPF_INTERFACE',
             'IP.ISIS_INSTANCE','IP.MPLS_GLOBAL','IP.MPLS_SR',
             'IP.EVPN_INSTANCE','IP.VXLAN_VNI','IP.SRV6_LOCATOR',
             'IP.BFD_SESSION','IP.VRRP_GROUP','IP.QOS',
             'IP.NAT_RULE','IP.DHCP_RELAY','IP.INTERFACE',
             'IP.ROUTE_POLICY','IP.ROUTE_TABLE'
           ]) AS node_id) core ON tagged.n_id = core.node_id
           WHERE tagged.n_id IS NULL"""
    )
    # Relation types with zero facts
    from src.ontology.registry import OntologyRegistry
    reg = OntologyRegistry.from_default()
    used_preds = {r["predicate"] for r in store.fetchall(
        "SELECT DISTINCT predicate FROM facts WHERE lifecycle_state='active'"
    )}
    unused = sorted(reg.relation_ids - used_preds)
    # Overall coverage stats
    total_nodes_row = graph.read(
        "MATCH (n:OntologyNode) WHERE n.lifecycle_state='active' RETURN count(n) AS cnt"
    )
    total_nodes = total_nodes_row[0]["cnt"] if total_nodes_row else 0
    tagged_row = store.fetchone(
        "SELECT count(DISTINCT ontology_node_id) AS cnt FROM segment_tags WHERE tag_type='canonical'"
    )
    tagged = tagged_row["cnt"] if tagged_row else 0
    return {
        "case": "knowledge_gap",
        "title": "知识空白发现（元认知）",
        "question": "我们对哪些领域的知识是空白的？系统知道自己不知道什么。",
        "why_unique": "这是传统系统完全做不到的。本系统通过对比本体定义和实际知识覆盖，精确定位哪些概念缺少知识、哪些关系类型从未被使用。",
        "coverage": {
            "total_ontology_nodes": total_nodes,
            "nodes_with_knowledge": tagged,
            "coverage_rate": round(tagged / max(total_nodes, 1), 4),
        },
        "nodes_without_facts": [dict(r) for r in no_facts],
        "core_nodes_without_segments": [dict(r) for r in no_segments_row],
        "unused_relation_types": unused[:20],
        "unused_relation_count": len(unused),
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