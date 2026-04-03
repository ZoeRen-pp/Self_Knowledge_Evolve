# 系统监控与知识质量看板 — 设计方案

**日期**：2026-04-04
**状态**：方案已确认，待开发

---

# 一、设计目标

构建系统级可观测性，覆盖知识规模、质量、图谱结构健康、本体工程健康等维度。支持：

1. 定时采集指标（每 5 分钟快照）
2. REST API 查询指标 + 异常下钻到具体知识条目
3. 前端看板可视化（趋势 + 异常高亮 + 可点击下钻）

---

# 二、架构位置

```
┌─ 上层入口 ────────────────────────────────────────────────┐
│  FastAPI                                                   │
│    ├─ /api/v1/semantic/*       → 15 + 4 个语义算子         │
│    ├─ /api/v1/system/stats     → 全量指标快照              │
│    ├─ /api/v1/system/drilldown/* → 异常下钻（纯路由层）    │
│    └─ /dashboard               → 前端看板                  │
│                                                            │
│  StatsCollector（独立模块，直接读 PG + Neo4j）             │
│    ├─ collect_all()            → 计算全量指标               │
│    └─ 定时写入 system_stats_snapshots 表                    │
│                                                            │
│  Drilldown（纯路由层，零 SQL/Cypher）                      │
│    └─ metric_name → app.query(operator, params)            │
└────────────────────────────────────────────────────────────┘
```

**关键约束**：
- `StatsCollector` 负责采集汇总指标，可以直接查 PG + Neo4j（统计聚合）
- `Drilldown` 是纯路由映射，**不允许包含任何 SQL/Cypher**，全部通过 `app.query()` 调现有或新增算子
- 新增的查询能力必须封装为标准算子（走 `SemanticOperator` + `OperatorRegistry`）

---

# 三、模块结构

```
src/stats/
├── __init__.py
├── collector.py          # 指标采集：直接读 PG + Neo4j，返回 dict
└── scheduler.py          # 定时任务：每 N 分钟调 collector，写 PG 快照表

src/stats/drilldown.py    # 纯路由：metric_name → (operator, params) 映射

src/api/system/
├── __init__.py
└── router.py             # FastAPI 端点：/stats, /stats/history, /drilldown/{metric}

static/
└── dashboard.html        # 单文件前端（HTML + JS + Chart.js CDN）

新增算子（标准流程注册）：
├── src/api/semantic/graph_inspect.py       # 图谱结构检查
├── src/api/semantic/cross_layer_check.py   # 五层覆盖率检查
├── src/api/semantic/ontology_inspect.py    # 本体工程检查
├── src/api/semantic/stale_knowledge.py     # 时间衰减查询
├── src/operators/graph_inspect_op.py
├── src/operators/cross_layer_check_op.py
├── src/operators/ontology_inspect_op.py
└── src/operators/stale_knowledge_op.py
```

---

# 四、指标体系（7 大类）

## 4.1 知识规模

| 指标 | 字段名 | 计算方式 |
|------|--------|----------|
| 文档按状态分布 | `documents_by_status` | `GROUP BY status` |
| 段落总数/活跃/去重 | `segments_total/active/superseded` | `GROUP BY lifecycle_state` |
| 事实总数/活跃/冲突/去重 | `facts_total/active/conflicted/superseded` | `GROUP BY lifecycle_state` |
| 证据总数 | `evidence_total` | `COUNT(*)` |
| RST 关系总数 | `rst_relations_total` | `COUNT(*)` |
| Neo4j 节点/关系数 | `neo4j_nodes/relationships` | Cypher `COUNT` |

## 4.2 知识质量

| 指标 | 字段名 | 计算 | 下钻 |
|------|--------|------|------|
| 本体覆盖率 | `ontology_coverage` | 被 Fact 引用的节点数 / 总节点数 | → `graph_inspect(isolated_nodes)` |
| 平均置信度 | `avg_fact_confidence` | `AVG(confidence)` | — |
| 低置信度占比 | `low_confidence_ratio` | `confidence < 0.5` 占比 | → `filter(fact, max_confidence=0.5)` |
| 冲突率 | `conflict_ratio` | conflicted / total | → `conflict_detect` |
| 段落去重率 | `segment_dedup_ratio` | superseded / total | — |
| 单源弱证据占比 | `single_evidence_weak_ratio` | 只有 1 条 evidence 且 rank ≤ B | → `stale_knowledge(weak_evidence=true)` |

## 4.3 来源分布

| 指标 | 字段名 | 下钻 |
|------|--------|------|
| 文档按等级 | `docs_by_rank` | → `filter(documents, rank=X)` |
| 文档按站点 | `docs_by_site` | → `filter(documents, site_key=X)` |
| 事实按抽取方式 | `facts_by_method` | → `filter(fact, extraction_method=X)` |

## 4.4 本体演化

| 指标 | 字段名 | 下钻 |
|------|--------|------|
| 候选按状态 | `candidates_by_status` | → `candidate_discover` |
| 待审核数 | `pending_review_count` | → `candidate_discover(status=pending_review)` |
| 自动晋升数 | `auto_accepted_count` | — |

## 4.5 Pipeline 健康

| 指标 | 字段名 | 下钻 |
|------|--------|------|
| 积压文档 | `backlog` | → `filter(documents, status=raw)` |
| 24h 处理量 | `processed_24h` | — |
| 失败文档 | `failed_count` | → `filter(documents, status=failed)` |

## 4.6 图谱结构健康

| 指标 | 字段名 | 计算 | 下钻 |
|------|--------|------|------|
| 孤立节点 | `isolated_nodes` | 零 RELATED_TO 边的本体节点 | → `graph_inspect(isolated_nodes)` |
| 超级节点 | `super_nodes` | 度 > 阈值 | → `graph_inspect(super_nodes, threshold=N)` |
| 度分布 | `degree_stats` | 平均/中位数/最大/标准差 | — |
| 谓语集中度 | `predicate_concentration` | 单节点边集中于单一谓语 | → `graph_inspect(predicate_concentration)` |
| 谓语利用率 | `predicate_utilization` | 已用 / 54 种 | → `graph_inspect(unused_predicates)` |
| 跨层覆盖率 | `cross_layer_coverage` | concept→mech, mech→method, method→cond, →scenario | → `cross_layer_check(src, tgt)` |
| 跨层断裂节点 | `cross_layer_gaps` | 某层节点完全没有与相邻层的关系 | → `cross_layer_check(gaps=true)` |
| 知识新鲜度 | `stale_fact_ratio` | evidence 最新时间 > N 天的 fact 占比 | → `stale_knowledge(days=N)` |
| 陈旧文档率 | `stale_doc_ratio` | 爬取时间 > N 天 | → `stale_knowledge(type=doc, days=N)` |
| 冲突集群深度 | `conflict_clusters` | 同一 S+P 有多少互相矛盾的 O | → `conflict_detect` |
| 未解决冲突存续 | `unresolved_conflict_age` | 平均存续天数 | — |

## 4.7 本体工程健康

| 指标 | 字段名 | 计算 | 下钻 |
|------|--------|------|------|
| 最大继承深度 | `max_inheritance_depth` | SUBCLASS_OF 最长链 | — |
| 平均分支因子 | `avg_branch_factor` | 子节点数均值 | — |
| 单子节点比例 | `single_child_ratio` | 只有 1 个子节点的父节点 | → `ontology_inspect(single_child)` |
| 无别名节点 | `no_alias_ratio` | 无 alias 的节点占比 | → `ontology_inspect(no_alias)` |
| 别名冲突 | `alias_conflicts` | 多节点共享 surface_form | → `ontology_inspect(alias_conflicts)` |
| 关系类型利用率 | `relation_type_utilization` | 同 4.6 predicate_utilization | → `graph_inspect(unused_predicates)` |

---

# 五、新增算子规格

## 5.1 graph_inspect

```
GET /api/v1/semantic/graph_inspect?inspect_type=isolated_nodes&limit=50

inspect_type:
  - isolated_nodes      → 零 RELATED_TO 边的本体节点列表
  - super_nodes         → 度 > threshold 的节点 + 度数 + 谓语分布 (threshold 参数)
  - degree_distribution → 全图度分布统计 (avg/median/max/stddev)
  - predicate_concentration → 边集中于单一谓语的节点
  - unused_predicates   → 54 种中未被 Fact 使用的谓语列表
```

## 5.2 cross_layer_check

```
GET /api/v1/semantic/cross_layer_check?source_layer=concept&target_layer=mechanism&gaps=true&limit=50

返回：
  - coverage: float (覆盖率)
  - gap_nodes: [{node_id, name, layer}]  (如 gaps=true)
```

## 5.3 ontology_inspect

```
GET /api/v1/semantic/ontology_inspect?inspect_type=no_alias&limit=50

inspect_type:
  - inheritance_stats   → {max_depth, avg_branch_factor, single_child_nodes}
  - no_alias            → 无别名节点列表
  - single_child        → 只有 1 个子节点的父节点列表
  - alias_conflicts     → 多节点共享同一 surface_form 的冲突列表
```

## 5.4 stale_knowledge

```
GET /api/v1/semantic/stale_knowledge?type=fact&days=90&limit=50

type:
  - fact → evidence 最新时间 > N 天的 facts
  - doc  → crawl_time > N 天的 documents
  - weak_evidence → 只有 1 条 evidence 且 rank ≤ B 的 facts
```

---

# 六、Drilldown 路由映射

```python
# src/stats/drilldown.py — 纯映射，零 SQL

METRIC_TO_QUERY = {
    # 知识质量
    "isolated_nodes":         ("graph_inspect",      {"inspect_type": "isolated_nodes"}),
    "low_confidence_facts":   ("filter",             {"object_type": "fact", "filters": {"max_confidence": 0.5}}),
    "single_evidence_weak":   ("stale_knowledge",    {"type": "weak_evidence"}),

    # 来源
    "docs_by_rank":           ("filter",             {"object_type": "documents"}),  # 外部传 rank 参数

    # 演化
    "pending_candidates":     ("candidate_discover", {"window_days": 365, "min_frequency": 1}),

    # Pipeline
    "backlog_docs":           ("filter",             {"object_type": "documents", "filters": {"status": "raw"}}),
    "failed_docs":            ("filter",             {"object_type": "documents", "filters": {"status": "failed"}}),

    # 图谱结构
    "super_nodes":            ("graph_inspect",      {"inspect_type": "super_nodes", "threshold": 50}),
    "unused_predicates":      ("graph_inspect",      {"inspect_type": "unused_predicates"}),
    "predicate_concentration":("graph_inspect",      {"inspect_type": "predicate_concentration"}),
    "cross_layer_gaps":       ("cross_layer_check",  {"gaps": True}),
    "stale_facts":            ("stale_knowledge",    {"type": "fact", "days": 90}),
    "stale_docs":             ("stale_knowledge",    {"type": "doc", "days": 90}),
    "conflict_clusters":      ("conflict_detect",    {}),  # 外部传 topic_node_id

    # 本体工程
    "no_alias_nodes":         ("ontology_inspect",   {"inspect_type": "no_alias"}),
    "single_child_nodes":     ("ontology_inspect",   {"inspect_type": "single_child"}),
    "alias_conflicts":        ("ontology_inspect",   {"inspect_type": "alias_conflicts"}),
}

def drilldown(metric_name: str, app, **override_params) -> dict:
    op_name, default_params = METRIC_TO_QUERY[metric_name]
    params = {**default_params, **override_params}
    return app.query(op_name, **params).data
```

---

# 七、数据存储

新增 PG 表（public schema）：

```sql
CREATE TABLE system_stats_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    snapshot    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 保留最近 7 天，定期清理
CREATE INDEX idx_stats_created ON system_stats_snapshots(created_at);
```

Scheduler 每 5 分钟写入一条快照。前端看板读最新快照 + 历史趋势。

---

# 八、前端看板

单文件 `static/dashboard.html`，纯 HTML + vanilla JS + Chart.js（CDN 引入），不需要 Node/React。

**布局**：

```
┌─────────────────────────────────────────────────────────┐
│  总量卡片：文档 | 段落 | 事实 | Neo4j 节点              │
├────────────────────────┬────────────────────────────────┤
│  质量仪表盘            │  来源分布饼图                   │
│  覆盖率 ████░░ 72%    │  S: 30  A: 13  B: 5            │
│  置信度 ████░░ 0.68   │                                 │
│  冲突率 █░░░░░ 4%     │  抽取方式：LLM 700 / Rule 6300  │
├────────────────────────┴────────────────────────────────┤
│  图谱健康                                                │
│  跨层覆盖热力图        孤立节点(点击下钻)                │
│  concept→mech: 45%     IP.LLDP ⚠                       │
│  mech→method:  32%     IP.QINQ ⚠                       │
│  超级节点: IP.BGP(342) IP.OSPF(210) ⚠                  │
├──────────────────────────────────────────────────────────┤
│  趋势图（过去 7 天）                                     │
│  ── 事实增量  ── 文档增量  ── 冲突增量                   │
└──────────────────────────────────────────────────────────┘
```

每个 ⚠ 指标可点击 → `fetch('/api/v1/system/drilldown/{metric}')` → 弹出具体条目列表。

---

# 九、API 端点汇总

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | `/api/v1/system/stats` | 最新一次指标快照 |
| GET | `/api/v1/system/stats/history?hours=24` | 历史快照列表（趋势图用） |
| GET | `/api/v1/system/drilldown/{metric_name}?limit=20` | 异常指标下钻到具体条目 |
| GET | `/api/v1/semantic/graph_inspect?inspect_type=...` | 新算子：图谱结构检查 |
| GET | `/api/v1/semantic/cross_layer_check?...` | 新算子：跨层覆盖检查 |
| GET | `/api/v1/semantic/ontology_inspect?inspect_type=...` | 新算子：本体工程检查 |
| GET | `/api/v1/semantic/stale_knowledge?type=...&days=...` | 新算子：知识时效查询 |
| GET | `/dashboard` | 前端看板页面 |

---

# 十、对现有代码的改动

| 改动 | 范围 |
|------|------|
| `scripts/init_postgres.sql` | 新增 `system_stats_snapshots` 表 |
| `src/stats/` | 新模块：collector + scheduler + drilldown |
| `src/api/system/` | 新 router |
| `src/api/semantic/` | 新增 4 个业务逻辑文件 |
| `src/operators/` | 新增 4 个算子 + 注册到 `__init__.py` |
| `src/api/semantic/router.py` | 新增 4 个端点 |
| `src/app.py` | 注册 system router + mount static + 启动 scheduler |
| `static/dashboard.html` | 新文件 |
| 现有 15 个算子 | **不改** |

算子总数：15 → 19。