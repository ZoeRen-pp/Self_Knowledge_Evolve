# Project Context for Claude Code

## 项目是什么

电信领域语义知识库（Telecom Semantic Knowledge Base）。

**不是 RAG，不是搜索引擎**。是一套有治理能力的、可演化的、带溯源的结构化知识基础设施，核心价值在于：
- 跨厂商术语归一化（华为/思科/中兴 → canonical ontology node）
- 故障影响链路推导（`impact_propagate` 算子）
- 知识溯源与置信度（每条 Fact 带 5 维置信度 + source_authority）
- 本体防漂移（`evolution_gate` 六项门控）
- 向量语义搜索（BAAI/bge-m3，pgvector，`semantic_search` / `edu_search` 算子）

## 技术栈

| 组件 | 用途 |
|------|------|
| semcore | 自研框架包（`semcore/`），零外部依赖，定义所有 ABC |
| FastAPI | REST API，15 个语义算子，通过 OperatorRegistry 分发 |
| PostgreSQL (telecom_kb) | 知识库：public schema（documents/segments/facts/evidence/…）+ governance schema（evolution_candidates/conflict_records/…） |
| PostgreSQL (telecom_crawler) | 爬虫库：source_registry/crawl_tasks/extraction_jobs |
| Neo4j | 图数据库，运行时本体 + 知识图谱，5 类节点 label |
| MinIO | 对象存储，原始爬取文档 |
| BAAI/bge-m3 | 向量嵌入，1024 维，中英双语，本地部署 |

## 层级关系

```
semcore ABCs（providers/base.py · operators/base.py · pipeline/base.py · app.py）
    ↑ implements
src/ 电信领域实现（providers/ · operators/ · governance/ · pipeline/stages/）
    ↑ wired by
src/app_factory.py → build_app() → SemanticApp singleton
    ↑ serves
src/app.py（FastAPI）→ src/api/semantic/router.py → OperatorRegistry
```

## 本体工程承载

```
YAML (ontology/)  →  source of truth，版本控制
  ↓ scripts/load_ontology.py
Neo4j             →  运行时投影，支撑图遍历（5 类 label）
PostgreSQL        →  版本治理和审核记录
OntologyRegistry  →  内存加速，pipeline 对齐使用
```

修改本体：先改 YAML，再跑 `load_ontology.py`，不要直接改 Neo4j。

## 五层知识模型

| 层 | YAML 文件 | Neo4j label | 数量 |
|---|---|---|---|
| concept | ip_network.yaml | OntologyNode | 66 |
| mechanism | ip_network_mechanisms.yaml | MechanismNode | 24 |
| method | ip_network_methods.yaml | MethodNode | 22 |
| condition | ip_network_conditions.yaml | ConditionRuleNode | 20 |
| scenario | ip_network_scenarios.yaml | ScenarioPatternNode | 13 |

## 项目结构速查

```
semcore/semcore/           ← 框架包（可独立发布）
  core/types.py            ← 所有领域数据类（OntologyNode, Fact, Segment …）
  core/context.py          ← PipelineContext（typed fields + stage_outputs + meta）
  providers/base.py        ← ABC：LLMProvider, EmbeddingProvider, GraphStore …
  ontology/base.py         ← OntologyProvider ABC
  governance/base.py       ← ConfidenceScorer, ConflictDetector, EvolutionGate ABCs
  operators/base.py        ← SemanticOperator, OperatorMiddleware, OperatorRegistry
  pipeline/base.py         ← Stage ABC，Pipeline（linear + branch + switch）
  app.py                   ← SemanticApp + AppConfig

src/
  app.py                   ← FastAPI entry point（生产）
  app_factory.py           ← build_app() + get_app() singleton
  config/settings.py       ← Pydantic settings，读 .env
  db/postgres.py           ← 知识库 PG 连接池
  db/crawler_postgres.py   ← 爬虫库 PG 连接池
  db/neo4j_client.py       ← Neo4j driver wrapper
  dev/                     ← 本地开发内存替代
    fake_postgres.py       ← SQLite :memory: 替代知识库 PG
    fake_crawler_postgres.py ← SQLite :memory: 替代爬虫库 PG
    fake_neo4j.py          ← dict 替代 neo4j driver
    seed.py                ← 从 YAML 本体 seed 假库
  pipeline/
    preprocessing/         ← 文本预处理（从 crawler 解耦）
      extractor.py         ← HTML 正文提取（trafilatura/readability/回退）
      normalizer.py        ← 去噪、归一化、hash
    stages/                ← stage1~stage6
    pipeline_factory.py    ← build_pipeline()
    runner.py              ← legacy 批量 runner
  providers/               ← semcore Provider 实现（postgres/crawler_postgres/neo4j/llm/embedding/minio）
  ontology/
    registry.py            ← 内存本体注册表（YAML loader）
    validator.py           ← YAML 校验
    yaml_provider.py       ← OntologyProvider 包装 registry
  governance/              ← TelecomConfidenceScorer / ConflictDetector / EvolutionGate
  operators/               ← 15 个 SemanticOperator（ALL_OPERATORS 在 __init__.py）
  api/semantic/            ← 算子业务逻辑（lookup.py … router.py）
  crawler/                 ← Spider（纯 HTTP 抓取 + 存 MinIO + 建 documents 记录，Pipeline 外部）
  utils/                   ← text / hashing / confidence / embedding / llm_extract / logging

scripts/
  init_postgres.sql        ← 知识库 DDL（public + governance schema）
  init_crawler_postgres.sql ← 爬虫库 DDL（source_registry/crawl_tasks/extraction_jobs）
  init_neo4j.py            ← 约束和索引
  load_ontology.py         ← YAML → Neo4j + PG lexicon
  migrations/              ← 002_merge_edu_into_segments / 003_governance_schema

ontology/
  top/relations.yaml       ← 54 种受控关系类型
  domains/                 ← 5 个 YAML，145 节点
  lexicon/aliases.yaml     ← 793 条别名（中英 + 厂商）
  governance/evolution_policy.yaml ← 演化门控阈值

docs/
  semcore-framework-design.md    ← 框架设计决策
  refactoring-plan.md            ← src/ 重构迁移计划
  architecture-decisions.md      ← ADR（embedding/storage/LLM 选型）
  telecom-semantic-kb-system-design.md
  telecom-ontology-design.md
  development-plan-detailed.md

run_dev.py                 ← 本地开发入口（无需 Docker，内存模式）
docker-compose.yml         ← PostgreSQL + Neo4j 容器
```

## 当前状态

- semcore 框架已实现并完成 src/ 重构，详见 `docs/refactoring-plan.md`
- 15 个算子均通过 OperatorRegistry + middleware 分发，入口统一在 `router.py`
- Embedding 代码已实现（`src/utils/embedding.py`，`src/providers/bge_m3_embedding.py`）；stage6 已写入逻辑；启用需设 `EMBEDDING_ENABLED=true` 并下载 `BAAI/bge-m3` 模型
- `semantic_search` 和 `edu_search` 两个向量检索算子已实现
- 本地开发模式已验证（`run_dev.py`），无需任何外部服务即可运行 `/lookup` `/resolve`
- Stage 4 抽取仍用正则，召回率有限；启用 `LLM_ENABLED=true` 可接 Claude API
- 知识冷启动：先跑 `load_ontology.py`，再灌文档数据
- **数据库已分库分 schema**：爬虫表 → telecom_crawler 独立库，治理表 → governance schema，t_edu_detail 已合并入 segments（详见 ADR-007）
- **RST 关系类型已扩展为 21 种**通用分类，按 6 个逻辑类别组织（详见 ADR-008）
- **爬虫已与 Pipeline 解耦**：Spider 在 Pipeline 外部负责抓取 + 建 documents 记录；Stage 1 纯清洗，接受 source_doc_id 入参，不感知数据来源（详见 ADR-009）

## 开发注意事项

- `.env` 存数据库连接，不要 commit
- 所有设计先写文档（`docs/`），确认后再写代码
- 所有语义算子统一入口：`src/api/semantic/router.py` → `app.query(op_name, **kwargs)`
- 新增算子流程：`src/api/semantic/xxx.py`（业务逻辑）→ `src/operators/xxx_op.py`（包装）→ `src/operators/__init__.py`（注册）→ `router.py`（endpoint）
- 置信度公式在 `src/utils/confidence.py`：`0.30×source_authority + 0.20×extraction_method + 0.20×ontology_fit + 0.20×cross_source_consistency + 0.10×temporal_validity`
- semcore 包通过 `sys.path.insert(0, "semcore")` 引用（未安装），或 `pip install -e semcore`（需 setuptools≥68）
- Docker 容器：Neo4j 和 PostgreSQL，连接配置在 `.env.example` 和 `docker-compose.yml`
- 本地无 Docker 调试：`python run_dev.py`，内存模式已通过 `/health`、`/lookup`、`/resolve` 验证
- 爬虫表（source_registry/crawl_tasks/extraction_jobs）在独立数据库 `telecom_crawler`，通过 `app.crawler_store` 访问；Pipeline stage 中操作这些表必须用 `crawler_store` 而非 `store`
- 治理表（evolution_candidates/conflict_records/review_records/ontology_versions）在 `governance` schema，SQL 中必须写 `governance.` 前缀；dev 模式 SQLite 自动剥离前缀