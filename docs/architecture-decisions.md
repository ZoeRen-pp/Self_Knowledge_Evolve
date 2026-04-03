# Architecture Decision Records

记录项目关键设计决策，供后续开发参考。

---

## ADR-001 本体模型的工程承载主体

**决策**：YAML 是本体的唯一源头，Neo4j 是运行时投影，PostgreSQL 负责治理。

**三层分工**：
```
YAML 文件 (ontology/)          → 源头，人工可读可编辑，版本控制跟踪
  ↓ scripts/load_ontology.py
Neo4j (OntologyNode / edges)   → 运行时，支撑图遍历算子
PostgreSQL (ontology_versions) → 治理，记录版本历史和审核状态
```

**为什么不用 Neo4j 直接定义 schema**：Neo4j 是 schema-less 图数据库，没有强制的类型系统；用 YAML 作 source of truth 可以让本体修改走 Git 审查流程，而不是直接改数据库。

---

## ADR-002 pgvector 当前未启用

**决策**：pgvector 暂不引入，当前所有语义查询通过 Neo4j 图遍历 + PostgreSQL 精确匹配完成。

**原因**：
- 现阶段知识库冷启动，数据量不足以支撑向量检索的精度优势
- 引入 embedding 需要额外的模型服务，增加运维复杂度
- 当前的精确匹配 + 图遍历已能覆盖核心业务场景

**未来接入点**（需要时再做）：
- `lookup` 算子加模糊语义查询兜底
- `stage3_align` 软对齐（精确未命中 → 向量相似度补充）
- `attach_score` 用向量相似度替代 Jaccard 关键词重叠

---

## ADR-003 Embedding 模型选型预研结论

**推荐模型**：`BAAI/bge-m3`

| 维度 | 评估 |
|------|------|
| 中英双语 | 强，专为中英混合训练 |
| 技术领域文本 | 学术/技术 benchmark 表现好 |
| 本地部署 | 支持，模型文件约 2.3GB |
| 内存需求 | ~3GB RAM（CPU 推理） |
| 费用 | 免费开源 |

**备选**：
- 内存受限 → `paraphrase-multilingual-MiniLM-L12-v2`（500MB，精度略低）
- 不想维护本地模型 → OpenAI `text-embedding-3-small`（加 `OPENAI_API_KEY` 即可）

**建议架构**：embedding 服务独立为一个 Docker 容器，对外暴露 `POST /embed`，主服务通过 HTTP 调用，解耦模型版本。

---

## ADR-004 系统定位与适用场景

**定位**：有治理能力、可演化、带溯源的电信领域结构化知识基础设施
- 不是 RAG，不是搜索引擎
- 核心价值在于知识的**可治理性、可溯源性、可演化性**

**真正有竞争力的场景**：

1. **跨厂商术语归一化** — 华为/思科/中兴同一概念统一到 canonical node，`resolve()` 算子直接支持
2. **故障影响链路推导** — `impact_propagate()` 沿 CAUSES/IMPACTS 边 BFS，给 NOC 提供机器可读的影响面
3. **依赖闭包分析** — `dependency_closure()` 用于变更前影响面评估
4. **知识溯源与置信度** — 每条 Fact 附带 source_authority + 5维置信度公式，区别于所有 RAG 系统
5. **本体防漂移** — `evolution_gate()` 六项门控，候选术语必须经过人工审核才进核心本体

**不适合的场景**：
- 一次性问答（直接用 LLM 更快）
- 通用知识（没有领域本体就没有优势）
- 文档量极少（图稀疏时算子无意义）

---

## ADR-005 Stage 4 抽取的已知局限

**当前实现**：15 条正则模式匹配关系抽取

**局限**：
- 只能抽取文本中明确出现 pattern 的关系，复杂语义关系漏掉
- 召回率有限，适合规范技术文档（RFC、配置指南），不适合叙述性文章

**后续改进方向**：接入 LLM 做关系抽取（传入段落 + 候选关系类型 → 结构化输出），正则作为快速通道保留。

---

## ADR-006 知识冷启动策略

**问题**：图谱节点稀疏时，图遍历算子（expand、path、impact）价值有限。

**建议优先级**：
1. 先跑 `scripts/load_ontology.py` 把 YAML 本体全量加载进 Neo4j（骨架）
2. 选 2-3 个核心子域（如 IP 路由、MPLS）的高质量文档（RFC + 主流厂商白皮书）跑完整 pipeline
3. 验证 `lookup` / `expand` / `path_infer` 三个算子有合理返回后，再扩大数据规模

---

## ADR-007 数据库分库分 schema

**决策**：将单一 PostgreSQL 数据库拆分为三个职责域。

**变更内容**：

1. **爬虫库独立**（telecom_crawler 数据库）
   - `source_registry`、`crawl_tasks`、`extraction_jobs` 迁入独立数据库
   - 新增 `src/db/crawler_postgres.py` 独立连接池
   - 新增 `src/providers/crawler_postgres_store.py`（RelationalStore 实现）
   - `AppConfig` / `SemanticApp` 新增 `crawler_store` 字段
   - Pipeline stage 中所有爬虫表操作走 `crawler_store`
   - `worker.py` 中跨库 JOIN 拆为两步查询
   - 配置项：`CRAWLER_POSTGRES_*`，默认复用主库的 host/user/password

2. **治理表分 schema**（governance schema，同一数据库）
   - `evolution_candidates`、`conflict_records`、`review_records`、`ontology_versions` 移入 `governance` schema
   - 所有 SQL 中加 `governance.` 前缀
   - 跨 schema 外键（如 `conflict_records.fact_id_a → public.facts.fact_id`）在同库内有效
   - dev 模式下 `_to_sqlite()` 自动剥离 `governance.` 前缀

3. **t_edu_detail 合并入 segments**
   - `segments` 表新增 `title`、`title_vec`、`content_vec`、`content_source` 四列
   - 删除 `t_edu_detail` 表
   - `t_rst_relation` 外键改为引用 `segments(segment_id)`

**为什么这样分**：
- 爬虫调度是高频写/短生命周期，知识存储是低频写/长生命周期，生命周期不同不该混在一起
- 治理表和知识表需要 JOIN（如 `conflict_records` 引用 `facts`），放同库不同 schema 既隔离命名又保留 JOIN 能力
- t_edu_detail 与 segments 1:1、文本完全重复，合并消除冗余

**迁移脚本**：
- `scripts/migrations/002_merge_edu_into_segments.sql`
- `scripts/migrations/003_governance_schema.sql`
- `scripts/init_crawler_postgres.sql`（新爬虫库 DDL）

---

## ADR-008 RST 关系类型扩展为 21 种通用分类

**决策**：将 RST 语篇关系类型从 11 种扩展为 21 种，按 6 个逻辑类别组织。

**为什么改**：
- 原 11 种类型过于粗放，无法区分因果方向（Cause-Result vs Result-Cause）、条件 vs 使能、对比 vs 让步等语义差异
- 技术文档中常见的"目的"（Purpose）、"手段"（Means）、"评价"（Evaluation）、"理由"（Justification）等关系没有覆盖

**新分类体系**：

| 类别 | 类型 |
|------|------|
| 因果逻辑 | Cause-Result, Result-Cause, Purpose, Means |
| 条件/使能 | Condition, Unless, Enablement |
| 展开/细化 | Elaboration, Explanation, Restatement, Summary |
| 对比/让步 | Contrast, Concession |
| 证据/评价 | Evidence, Evaluation, Justification |
| 结构/组织 | Background, Preparation, Sequence, Joint, Problem-Solution |

**影响范围**：
- `src/utils/llm_extract.py`：`RST_RELATION_TYPES` 列表 + LLM system prompt
- `src/pipeline/stages/stage2_segment.py`：`_RULE_RST` 规则映射（13 条 → 37 条）
- 未匹配的 segment type 组合默认仍为 `Sequence`
- `t_rst_relation.relation_type` 字段为 VARCHAR(255)，无需改表结构

---

## ADR-009 爬虫与 Pipeline 解耦

**决策**：爬虫（Spider）是 Pipeline 的外部数据源之一，不是 Pipeline 的一部分。Pipeline 的入参统一为 `source_doc_id`。

**变更前**：
```
Spider.fetch() → crawl_tasks.status='done'
    ↓
worker 传 crawl_task_id → Pipeline
    ↓
Stage 1 (Ingest): 读 crawl_tasks → 创建 documents → 抽取正文 → 清洗 → 质量检查
```

Stage 1 同时承担了"数据源接入"和"文档清洗"两个职责，且只能处理爬虫来源。

**变更后**：
```
[数据源层]                         [Pipeline 层]
Spider → MinIO + documents(raw)
文件导入 → MinIO + documents(raw)    →  Stage 1: 纯清洗（source_doc_id）
API 上传 → MinIO + documents(raw)        raw → extract → normalize → cleaned
                                     →  Stage 2: 切段 ...
```

- Spider 爬完后自己创建 `documents` 记录（`status='raw'`）+ 写 raw 到 MinIO
- Pipeline 只接受 `source_doc_id`，从 documents 表 + MinIO 开始
- Stage 1 变成纯清洗阶段：加载 raw → 正文提取 → 去噪归一化 → 质量门控 → 存 cleaned → 更新 documents
- 新增数据源只需要：往 documents 表插记录 + 往 MinIO 放 raw 文件，不碰 Pipeline 代码

**文件变更**：
- `src/crawler/extractor.py` + `normalizer.py` → 移到 `src/pipeline/preprocessing/`（它们是文本处理，不是爬虫）
- `src/crawler/spider.py`：新增 `_create_document()` 方法，爬完后建 documents 记录
- `src/pipeline/stages/stage1_ingest.py`：重写为纯清洗，入参 `source_doc_id`
- `worker.py`：pipeline 用 `source_doc_id` 驱动，不再传 `crawl_task_id`
- `src/pipeline/runner.py`：同上

**文档状态流转**：`raw → cleaned → segmented → indexed`

---

## ADR-010 本体种子关系缺失（待解决）

**问题**：当前本体 YAML 只定义了节点 + 层次关系（SUBCLASS_OF） + 关系类型词典（54 种），但没有声明节点之间的具体关系实例。所有 RELATED_TO 边完全依赖 Pipeline 从文档中抽取。

**表现**：
- 54 种定义的关系类型中，实际只用到 18 种（1/3）
- 图中 RELATED_TO 边集中在 `uses_protocol` / `part_of` / `depends_on`
- `configured_by`、`isolates`、`peers_with`、`mounted_on` 等关系类型从未出现
- 一些公理级别的关系（如 "BGP is_a ROUTING_PROTOCOL"）不应该依赖文档发现

**根因**：
1. 本体 YAML 是框架（schema），不是实例（data）——缺少种子关系
2. 当前语料以 RFC 规范为主，不涉及运维/配置类关系
3. 抽取模式对部分关系类型的语言表述覆盖不足

**计划修复（待本轮爬虫完成后）**：

路 A — 本体加种子关系：
- YAML 节点定义中新增 `seed_relations` 字段，声明确定性的核心关系
- `load_ontology.py` 加载时直接在 Neo4j 中建边（作为 Fact with confidence=1.0）
- 这些关系是公理，不需要从文档发现

路 B — 扩大语料覆盖：
- 增加配置指南、故障手册、最佳实践类文档源
- 这类文档自然会触发 `configured_by`、`monitors`、`isolates` 等关系抽取

两条路都需要走。

---

## ADR-010 查询 API 尚未支持跨存储关联查询（遗留问题）

**背景**：当前 15 个语义算子中，大多数只查单一存储：

- 图遍历类（`expand`、`path`、`impact_propagate` 等）：纯 Neo4j
- 治理类（`evidence_rank`、`conflict_detect`）：纯 PostgreSQL
- 向量类（`semantic_search`、`edu_search`）：纯 PostgreSQL（pgvector）
- MinIO：**没有任何查询算子读取 MinIO**，目前仅写入路径（crawler/pipeline）

`expand` 虽然有 `include_segments=True` 参数，但它查的是 Neo4j 里的 `KnowledgeSegment` 节点（只有 segment_id + 元数据），不连接 PG 的 `segments` 表，无法返回 EDU 文本内容，也没有利用 PG 里存储的 RST 顺序关系。

**缺口**：流程性知识（配置步骤、操作顺序）存储在 PG `segments` 表的 RST 关系字段中，但没有查询 API 能将"Neo4j 图节点 → PG EDU 序列 → MinIO 原始段落"串联起来。

**待实现**：新增 `node_context` 算子，执行三层关联查询：
1. **Neo4j**：`(KnowledgeSegment)-[:TAGGED_WITH]->(OntologyNode {node_id})` 取 segment_id 列表
2. **PG**：按 segment_id 拉取 EDU 文本 + RST 关系 + sequence_order，重建顺序
3. **MinIO**（可选）：按 source_doc_id 取 `cleaned/` 原文，提供上下文窗口

**触发条件**：等有真实数据入库后，结合实际查询场景确认接口设计再实现。
