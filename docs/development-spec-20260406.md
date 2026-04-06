# 开发规格说明

日期：2026-04-06
版本：v0.4
状态：当前实现

## 1. 技术栈

| 组件 | 技术 | 用途 |
|------|------|------|
| Web 框架 | FastAPI | REST API + Dashboard 静态文件服务 |
| 知识数据库 | PostgreSQL (telecom_kb) | 文档、段落、知识条目、证据链、治理审计 |
| 爬虫数据库 | PostgreSQL (telecom_crawler) | 独立部署，站点注册、爬取任务队列 |
| 图数据库 | Neo4j Community | 五层本体 + 知识关系图 |
| 对象存储 | MinIO | 原始 HTML + 清洗后文本 |
| Embedding | Ollama (bge-m3, 1024维) | 语义搜索、去重、匹配、相似度 |
| LLM | 可配置 (DeepSeek/Claude/Gemini) | 关系抽取、候选词分类、描述生成 |
| 框架抽象 | semcore (自研) | 零依赖 ABCs，可独立发布 |
| 前端 | 单文件 HTML + Chart.js | Dashboard 5 Tab 面板 |
| Python | 3.12+ | 主语言 |

## 2. 模块清单

### 2.1 API 层（src/api/）

#### 语义算子 API（src/api/semantic/router.py）

21 个 REST 端点，统一响应格式 `{"meta": {"ontology_version", "latency_ms"}, "result": {...}}`：

| 端点 | 方法 | 业务文件 | 算子 |
|------|------|----------|------|
| `/lookup` | GET | lookup.py | lookup_op.py |
| `/resolve` | GET | resolve.py | resolve_op.py (未实现) |
| `/expand` | GET | expand.py | expand_op.py |
| `/path` | GET | path.py | path_op.py |
| `/dependency_closure` | GET | dependency.py | dependency_op.py |
| `/impact_propagate` | POST | impact.py | impact_op.py |
| `/filter` | GET | filter.py | filter_op.py |
| `/evidence_rank` | GET | evidence.py | evidence_op.py |
| `/conflict_detect` | POST | evidence.py | evidence_op.py |
| `/fact_merge` | POST | evidence.py | evidence_op.py |
| `/candidate_discover` | POST | evolution.py | evolution_op.py |
| `/attach_score` | POST | evolution.py | evolution_op.py |
| `/evolution_gate` | POST | evolution.py | evolution_op.py |
| `/context_assemble` | POST | context_assemble.py | context_assemble_op.py |
| `/semantic_search` | POST | (via operator) | search_op.py |
| `/ontology_quality` | GET | ontology_quality.py | ontology_quality_op.py |
| `/stale_knowledge` | GET | stale_knowledge.py | stale_knowledge_op.py |

#### 系统管理 API（src/api/system/router.py）

| 端点 | 方法 | 功能 |
|------|------|------|
| `/stats` | GET | 最新监控快照 |
| `/stats/history` | GET | 历史快照（趋势图） |
| `/drilldown/{metric}` | GET | 21 种指标钻取 |
| `/drilldown` | GET | 可用钻取指标列表 |
| `/pipeline_flow` | GET | 流水线各阶段数量 |
| `/recent_activity` | GET | 最近审核记录 + 文档 |
| `/candidate_distribution` | GET | 候选词来源次数分布 |
| `/review` | GET | 候选词列表（支持类型/状态过滤） |
| `/review/{id}` | GET | 候选词详情 + 关联文本段落 |
| `/review/{id}/approve` | POST | 审批（写 Neo4j + PG + YAML + Git） |
| `/review/{id}/reject` | POST | 拒绝 |
| `/review/merge` | POST | 合并多个候选词 |
| `/review/check_synonyms` | POST | 同义词检测（Embedding + LLM） |

### 2.2 Pipeline 层（src/pipeline/）

#### pipeline_factory.py

组装 7 阶段 Pipeline，支持按 doc_type 的 switch 路由（预留 RFC/CLI 专用分割器）。

#### Stage 实现

| Stage | 文件 | 核心方法 |
|-------|------|----------|
| 1 Ingest | stage1_ingest.py | `process()` → 加载 MinIO → 清洗 → 质量门控 |
| 2 Segment | stage2_segment.py | `_segment_doc()` → 三级切分 + RST + 语义角色 |
| 3 Align | stage3_align.py | `align_segment()` → 精确匹配 + Embedding 兜底 + LLM 候选发现 |
| 3b Evolve | stage3b_evolve.py | `_score_candidates()` + `_gate_and_promote()` |
| 4 Extract | stage4_extract.py | LLM 优先 → 合并重试 → 共现兜底 |
| 5 Dedup | stage5_dedup.py | SimHash + 精确匹配 + Embedding 语义去重 + 冲突检测 |
| 6 Index | stage6_index.py | 置信度门控 → Neo4j MERGE (动态关系类型) |

### 2.3 治理层（src/governance/）

| 文件 | 职责 |
|------|------|
| confidence_scorer.py | 五维置信度评分 (source_authority + extraction_method + ontology_fit + cross_source + temporal) |
| conflict_detector.py | 冲突检测：精确 (S+P 同, O 异) + Embedding 语义 (S+O 相似, P 异) |
| evolution_gate.py | 六道演化门控 (source_count, diversity, stability, fit, synonym_risk, composite) |
| maintenance.py | 周期性维护：Pass1 Embedding 去重 → Pass2 LLM 分类 → Pass3 清理(删 noise, 合并 variant, 清 stale) |

### 2.4 监控层（src/stats/）

| 文件 | 职责 |
|------|------|
| collector.py | StatsCollector: 7 类指标采集 (knowledge, quality, graph, ontology, pipeline, evolution, sources) |
| scheduler.py | StatsScheduler: 定时触发采集，写入 system_stats_snapshots |
| ontology_quality.py | OntologyQualityCalculator: 5 维度 20+ 指标，含 Embedding 节点相似度 |
| drilldown.py | 21 种钻取指标，纯路由层（调用语义算子，不含 SQL/Cypher） |
| backfill.py | BackfillWorker: 审批新概念后回填已有 segments 的标签 |

### 2.5 本体层（src/ontology/）

| 文件 | 职责 |
|------|------|
| registry.py | OntologyRegistry: 从 YAML 加载到内存，alias_map, nodes, relation_ids, 种子关系, 模式 |
| validator.py | 本体完整性校验 |
| yaml_provider.py | YAML 读写封装 |

### 2.6 工具层（src/utils/）

| 文件 | 职责 |
|------|------|
| embedding.py | Embedding 客户端：自动检测 Ollama → sentence-transformers 兜底 |
| llm_extract.py | LLMExtractor: 关系抽取 + 候选词分类 + RST 关系 + 标题生成 |
| normalize.py | normalize_term: 保留词边界的归一化 + 复数处理 + tokenize_normalized |
| confidence.py | 置信度计算公式 |
| hashing.py | SimHash + Jaccard + Hamming |
| text.py | 文本归一化 |
| health.py | 启动健康检查 |
| logging.py | 日志配置 |

### 2.7 存储层（src/providers/ + src/db/）

| 文件 | 职责 |
|------|------|
| postgres_store.py | PostgreSQL RelationalStore 实现 (telecom_kb) |
| crawler_postgres_store.py | PostgreSQL RelationalStore 实现 (telecom_crawler) |
| neo4j_store.py | Neo4j GraphStore 实现 |
| minio_store.py | MinIO ObjectStore 实现 |
| anthropic_llm.py | LLM Provider (ClaudeLLMProvider) |

### 2.8 Worker（worker.py）

4 线程守护进程：

```python
threads = [
    Thread(target=_crawler_thread, ...),      # 持续爬取
    Thread(target=_pipeline_thread, ...),      # 持续处理 raw 文档
    Thread(target=_stats_thread, ...),         # 每 5 分钟采集统计
    Thread(target=_maintenance_thread, ...),   # 每 24 小时本体维护
]
```

启动时自动注册种子 URLs（8 个站点，80+ seed URLs）。

### 2.9 脚本（scripts/）

| 脚本 | 功能 |
|------|------|
| reset_and_run.py | 原子化重置：杀进程→清数据→验证→加载本体→启动 Worker+API |
| load_ontology.py | YAML → Neo4j + PG lexicon（冷启动） |
| clean_candidates.py | 手动触发 OntologyMaintenance (CLI 包装器) |
| export_dashboard.py | 导出 Dashboard 为离线单文件 HTML |
| migrate_normalized_forms.py | 一次性迁移：旧归一化 → 新归一化（保留词边界） |
| init_neo4j.py | 初始化 Neo4j 约束和索引 |

### 2.10 Dashboard（static/dashboard.html）

5 Tab 单页应用，Chart.js 图表：

| Tab | 数据来源 | 交互 |
|-----|----------|------|
| 系统总览 | /pipeline_flow, /stats, /recent_activity | 流水线图、来源饼图 |
| 知识探索 | /lookup, /context_assemble | 搜索 → 节点+facts+推理链+源文本 |
| 质量评估 | /ontology_quality | 雷达图，每个指标可点击钻取详情 |
| 知识演化 | /candidate_distribution, /review | 分布图、审核操作 |
| 运行监控 | /stats, /stats/history | 趋势图、质量仪表盘 |

支持离线导出（`scripts/export_dashboard.py`）：预置 6 个术语的完整知识包。

## 3. 本体 YAML 结构

### 3.1 节点定义（ontology/domains/）

```yaml
# ip_network.yaml (概念层示例)
nodes:
  - id: IP.BGP
    canonical_name: BGP
    display_name_zh: 边界网关协议
    parent_id: IP.ROUTING_PROTOCOL
    maturity_level: core
    description: "Path-vector EGP for inter-AS routing. RFC 4271."
    aliases: [Border Gateway Protocol, BGP4, BGP-4, 边界网关协议]
    allowed_relations: [uses_protocol, establishes, advertises, depends_on]
    source_basis: [IETF]
    lifecycle_state: active
```

五层分别在 6 个文件中（含 ip_network_evolved.yaml 存放审批通过的演化节点）。

### 3.2 关系定义（ontology/top/relations.yaml）

```yaml
relations:
  - id: uses_protocol
    category: operational
    description: "A mechanism or method uses a specific protocol"
    domain_hint: mechanism|method
    range_hint: concept
    symmetric: false
    transitive: false
```

71 种关系，含 `category: evolved` 的审批通过新关系。

### 3.3 别名词典（ontology/lexicon/aliases.yaml）

871 条别名，含中英文和厂商变体。审批通过的新别名追加在此（alias_type: evolved）。

## 4. 配置项（src/config/settings.py）

所有配置通过环境变量覆盖：

| 配置组 | 关键项 |
|--------|--------|
| PostgreSQL | postgres_dsn, crawler_postgres_dsn |
| Neo4j | NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, NEO4J_DATABASE |
| MinIO | MINIO_ENDPOINT, MINIO_ACCESS_KEY, MINIO_SECRET_KEY, MINIO_BUCKET_RAW, MINIO_BUCKET_CLEANED |
| LLM | LLM_ENABLED, LLM_API_KEY, LLM_API_BASE, LLM_MODEL |
| Embedding | EMBEDDING_ENABLED, EMBEDDING_MODEL, EMBEDDING_DEVICE, EMBEDDING_DIM |
| Ollama | OLLAMA_URL, OLLAMA_EMBED_MODEL |
| Worker | WORKER_CRAWL_LIMIT, WORKER_PIPELINE_LIMIT, WORKER_SLEEP_SECS |
| 维护 | ONTOLOGY_MAINTENANCE_INTERVAL_HOURS, ONTOLOGY_MAINTENANCE_ENABLED |
| 启动 | LOG_LEVEL, STARTUP_HEALTH_REQUIRED |

## 5. 关键设计决策

### 5.1 数据库分离

知识库 (telecom_kb) 和爬虫库 (telecom_crawler) 独立部署，无跨库外键。Pipeline 通过 `source_doc_id` 关联。

### 5.2 治理 schema 隔离

governance 表使用独立 schema（`governance.evolution_candidates` 等），与知识表隔离。开发模式 SQLite 自动剥离 schema 前缀。

### 5.3 本体真相来源

YAML 是唯一真相来源。Neo4j 和 PG 是运行时投影。`scripts/load_ontology.py` 做单向同步。审批通过的演化写入 YAML + Git commit。

### 5.4 无 regex 抽取

Stage 4 不使用正则抽取关系，完全依赖 LLM。LLM 不可用时降级为共现（严格限制：恰好 2 节点 + 1 谓语 → 最多 1 条 fact）。

### 5.5 模式外部化

所有正则模式（语义角色、上下文信号、谓语信号、停用词）存放在 YAML 中，本体变化不改代码。

### 5.6 Embedding 优先 Ollama

`src/utils/embedding.py` 自动检测：Ollama 可用则用 Ollama API（无 Python 依赖、推理快），否则 fallback 到 sentence-transformers。

### 5.7 候选词归一化保留词边界

`normalize_term` 输出空格分隔的 token（非连接 blob），支持后续 token 级操作（Jaccard、包含检测、可读性）。

### 5.8 三层候选词过滤

Pipeline 粗筛 → Maintenance 精筛 → Human 终审。Noise 直接删除，variant 合并知识到原有节点，new_concept 保留待人工审批。

## 6. 开发约定

### 新增算子

1. `src/api/semantic/xxx.py` — 业务逻辑
2. `src/operators/xxx_op.py` — SemanticOperator 包装
3. `src/operators/__init__.py` — 注册到 ALL_OPERATORS
4. `src/api/semantic/router.py` — FastAPI 端点

### 新增 Pipeline Stage

1. `src/pipeline/stages/stageN_xxx.py` — 继承 Stage
2. `src/pipeline/pipeline_factory.py` — 加入链

### 本体变更

1. 编辑 `ontology/domains/*.yaml` 或 `ontology/top/relations.yaml`
2. `python scripts/load_ontology.py` 同步
3. 不要直接编辑 Neo4j

### 日志

所有新代码必须同步加日志，不事后补。使用 `log = logging.getLogger(__name__)`。

### 代码质量

- 禁止特例硬代码、魔鬼数字、无法泛化的逻辑
- 禁止写非明确要求的兼容性代码
- 新需求首先考虑架构位置，不产生耦合/混杂/职责不清