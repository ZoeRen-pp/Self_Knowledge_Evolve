# 语义知识操作系统 — 架构设计文档

**版本**：v0.4
**日期**：2026-04-04

---

# 1. 系统定位与核心价值

面向网络通信领域的**可治理、可演化、带溯源的语义知识基础设施**。

核心能力：
- 本体锚定的知识组织（5 层 153 节点 + 54 种关系 + 156 别名 + 104 种子关系）
- 多源数据接入（爬虫/文件/API 均在 Pipeline 外部，通过 documents 表 + MinIO 解耦）
- 7 阶段处理流水线（清洗 → 语义切段 → 对齐 → 演化 → LLM 抽取 → 去重 → 索引）
- 21 种 RST 语篇关系（6 大类）
- 20 个语义算子 API + 系统监控看板 + 本体质量评估（5 维 20 指标）
- 候选审批 + 增量回填

---

# 2. 总体逻辑架构

```
┌─ 上层入口 ──────��───────────────────────────────────────────────────────────┐
│  FastAPI REST API                        worker.py                          │
│    ├─ /api/v1/semantic/*  (20 算子)        └─ 爬取 + Pipeline 调度           │
│    ├─ /api/v1/system/*    (监控+审批)                                       │
│    └─ /dashboard          (看板 3 标签页)                                   │
│         ↓                                      ↓                            │
│  ┌─ SemanticApp ──────────────────────────────────────────────────────────┐ │
│  │  app.query(op_name)          app.ingest(source_doc_id)                 │ │
│  │       ↓                           ↓                                    │ │
│  │  OperatorRegistry (20)       Pipeline (7 stages)                       │ │
│  │  + TimingMiddleware          linear / branch / switch                   │ │
│  │  + LoggingMiddleware                                                   │ │
│  └───────��─────────────────────────────────��──────────────────────────────┘ │
└─────────��────────────────────────────────��──────────────────────────────────┘

┌─ 领域实现层 ──────────────────────────────────────────────────────────────────┐
│                                                                               │
│  ┌─ 本体层（统一语义骨架）──────────────────────────────────────────────────┐ │
│  │  YAML (ontology/) ── 唯一源头                                            │ │
│  │    domains/    → 153 节点（5 层）                                        │ │
│  │    seeds/      → 104 种子关系 + 3 分类修正                               │ │
│  │    patterns/   → 语义角色 + 上下文信号 + 谓语信号（外部化正则）          │ ��
│  │    lexicon/    → 156 别名                                                │ │
│  │    top/        → 54 种受控关系类型                                       ��� │
│  │       ↓ load_ontology.py + OntologyRegistry                              │ │
│  │  Neo4j（动态边类型） · PG lexicon_aliases · 内存 alias_map               │ │
│  └──────────���───────────────────────────────────────────────────────────────┘ │
│                                                                               │
│  ┌─ 治理层 ────────────────┐  ┌─ 流水线层 ──────────────────────────────┐   │
│  │ ConfidenceScorer        │  │ preprocessing/                          ���   │
│  │   5 维置信度加权         │  │   extractor + normalizer                │   │
│  │ ConflictDetector        │  │ stages/                                 │   │
│  │   same S+P diff O       │  │   1.清洗 → 2.语义切段+RST → 3.对齐     │   │
│  │ EvolutionGate           │  │   → 3b.演化 → 4.LLM抽取 → 5.去重      │   ���
│  │   6 项门控              │  │   → 6.索引                              │   │
│  └─────────────────────────┘  └���─────────────────────────��──────────────┘   │
│                                                                               │
│  ���─ 算子层 (20 个 SemanticOperator) ──────────────────────────────────────┐ │
│  │ 查询: lookup · resolve · expand · filter · path · dependency_closure    │ │
│  │ 分析: impact_propagate · evidence_rank · conflict_detect · fact_merge   │ │
│  │ 演化: candidate_discover · attach_score · evolution_gate                │ │
│  │ 搜索: semantic_search · edu_search                                      │ │
│  │ 检查: graph_inspect · cross_layer_check · ontology_inspect              │ │
���  │       stale_knowledge · ontology_quality                                │ │
│  └───────────────────────────────────────────��────────────────────────────┘ │
│                                                                               │
│  ┌─ 监控层 ──────────────────────────────────────────────────────────────┐  │
│  │ StatsCollector (7 类指标)  · StatsScheduler (5 min)                    │ │
│  │ OntologyQualityCalculator (5 维 20 指标)                               │ │
│  │ Drilldown (21 指标 → 算子映射)                                         │ │
│  │ Review API (审批 + BackfillWorker)                                     │ │
│  └────────────────────────────────────────���───────────────────────────────┘ │
└─────────────────────────────────────��─────────────────────────────────────────┘

┌─ semcore 框架层（零外部依赖 ABCs）─────���──────────────────────────────────────┐
│  providers/base.py   LLM · Embedding · GraphStore · RelationalStore · Object  │
│  ontology/base.py    OntologyProvider                                         │
│  governance/base.py  ConfidenceScorer · ConflictDetector · EvolutionGate      │
│  operators/base.py   SemanticOperator · OperatorMiddleware · OperatorRegistry │
│  pipeline/base.py    Stage · Pipeline (Linear/Branch/Switch)                  │
│  core/types.py       OntologyNode · Document · Segment · Fact · Evidence …    │
│  app.py              AppConfig → SemanticApp (组合根)                         │
└─────���─────────────────────────────────��──────────────────────────────────────┘

┌─ 基础设施层 ─���───────────────────────────────────────────────────────────────┐
│  PG telecom_kb (public + governance)  Neo4j (动态边类型)  MinIO (raw+cleaned)│
│  PG telecom_crawler                   BAAI/bge-m3 (1024d)  DeepSeek LLM     │
└──────────────────────────────────────��──────────────────────────��────────────┘

┌─ 外部数据源（Pipeline 之外）─���──────────────────────────────────────���────────┐
│  Spider 爬虫 · 文件导入 · API 上传  → MinIO(raw/) + documents(status='raw')  │
└────��────────────────────────────────��────────────────────────────────────────┘
```

---

# 3. 数据库架构

## 3.1 PostgreSQL telecom_kb — public schema (7 表)

| 表 | 职责 |
|----|------|
| documents | 文档元数据 + 状态流转 (raw→cleaned→segmented→indexed) |
| segments | 知识片段 + EDU + 向量 (title/title_vec/content_vec/content_source) |
| t_rst_relation | RST 语篇关系 (21 种，src_edu_id/dst_edu_id → segments) |
| segment_tags | 本体标签 (canonical/semantic_role/context + 五层标签) |
| facts | 三元组知识 (S,P,O + confidence + lifecycle_state) |
| evidence | 事实溯源 (fact→segment→document, extraction_method: llm/cooccurrence) |
| lexicon_aliases | 本体别名镜像 |
| system_stats_snapshots | 监控快照 (JSONB, 7 天保留) |

## 3.2 PostgreSQL telecom_kb — governance schema (4 表)

| 表 | 职责 |
|----|------|
| evolution_candidates | 统一候选池 (candidate_type: concept/relation, examples JSONB) |
| conflict_records | 矛盾事实记录 |
| review_records | 审批操作审计 |
| ontology_versions | 本体版本 + 变更差异 |

## 3.3 PostgreSQL telecom_crawler (3 表)

| 表 | 职责 |
|----|------|
| source_registry | 站点注册 (site_key, source_rank, seed_urls) |
| crawl_tasks | 爬取任务队列 |
| extraction_jobs | 流水线任务追踪 |

## 3.4 Neo4j

**节点标签**（多标签）：OntologyNode + MechanismNode/MethodNode/ConditionRuleNode/ScenarioPatternNode, Alias, Fact, Evidence, KnowledgeSegment, SourceDocument

**边类型**（动态）：
- 结构边：SUBCLASS_OF, ALIAS_OF, BELONGS_TO, TAGGED_WITH, SUPPORTED_BY, EXTRACTED_FROM
- 知识边：动态类型 (USES_PROTOCOL, DEPENDS_ON, EXPLAINS, IMPLEMENTS, ENCAPSULATES, ...)
  - MERGE by (src, type, dst) — 不会重复
  - r.confidence 取最大值，r.fact_count 累加支持数
  - r.source = 'ontology_seed' 区分种子关系

---

# 4. 7 阶段流水线

| Stage | 输入 | 处理 | 输出 |
|-------|------|------|------|
| 1 清洗 | source_doc_id | 从 MinIO 取 raw → trafilatura/readability 提取 → 去噪 → 质量门控 → doc_type | documents(cleaned) + MinIO(cleaned/) |
| 2 切段 | cleaned text | **三级切分**：段落边界 → 句号边界(贪心合并) → 滑窗兜底；语义角色分类(YAML patterns)；RST 关系(21种,LLM优先) | segments + t_rst_relation |
| 3 对齐 | segments | alias_map 匹配 → 五层标签；**LLM 候选概念发现** → regex 兜底 | segment_tags + evolution_candidates(concept) |
| 3b 演化 | candidates | 五维评分 → 六项门控 → auto_accept/pending_review | evolution_candidates 更新 |
| 4 抽取 | segments+tags | **LLM 优先** → merged context retry → co-occurrence 兜底；**无正则**；未知 predicate → evolution_candidates(relation) | facts + evidence |
| 5 去重 | facts+segments | SimHash 段落去重；精确 (S,P,O) 合并；冲突检测 | merge_cluster + conflict_records |
| 6 索引 | all | 置信度门控 → Neo4j(动态边类型,无重复) → 向量嵌入 | Neo4j + documents(indexed) |

---

# 5. 知识治理

**置信度**：`0.30×source_authority + 0.20×extraction_method + 0.20×ontology_fit + 0.20×cross_source + 0.10×temporal`

**六项门控**：source_count≥3, diversity≥0.6, stability≥0.7, fit≥0.65, composite≥0.65, synonym_risk≤0.4

**候选审批**：
- GET /api/v1/system/review → 列出候选
- POST /review/{id}/approve → 写入本体 + 版本 bump + 触发 BackfillWorker
- POST /review/{id}/reject → 标记拒绝

**增量回填**：新概念审批后，BackfillWorker 后台线程搜索已有 segments → 补 tag → LLM 抽 facts → 索引 Neo4j

---

# 6. 系统监控

**StatsCollector** 7 类指标：知识规模、质量、来源、演化、Pipeline、图谱健康、本体健康

**OntologyQualityCalculator** 5 维 20 指标：
- 粒度 (G1-G5)：Gini、超级节点、孤立率、标签密度、万金油
- 正交性 (O1-O4)：谓语共现 Jaccard、偏度、集中度、利用率
- 层间 (L1-L3)：覆盖率、短路率、完整路径
- 可发现性 (D1-D4)：别名覆盖、关系利用、标签命中
- 结构 (S1-S5)：联通、环、传递、对称、路径长

**Dashboard** 3 标签页：Monitor (实时) / Ontology Quality (雷达图) / Review (审批)

---

# 7. 20 个语义算子

| 分类 | 算子 |
|------|------|
| 查询解析 | lookup, resolve |
| 图遍历 | expand, path, dependency_closure |
| 影响分析 | impact_propagate, filter |
| 证据治理 | evidence_rank, conflict_detect, fact_merge |
| 本体演��� | candidate_discover, attach_score, evolution_gate |
| 语义搜索 | semantic_search, edu_search |
| 结构检查 | graph_inspect, cross_layer_check, ontology_inspect, stale_knowledge, ontology_quality |

---

# 8. 部署

**开发模式**：`python run_dev.py` — SQLite + dict 替代，零外部依赖

**生产模式**：
- PG telecom_kb + telecom_crawler (可同实例不同 database)
- Neo4j
- MinIO
- FastAPI (uvicorn)
- worker.py (爬取 + Pipeline)
- DeepSeek LLM (可选 BAAI/bge-m3 嵌入)