# 电信语义知识库 — 开发规格说明
**Python 实现（FastAPI + semcore 框架）**
**日期：2026-04-06 | 版本：1.0**

---

## 1. 技术栈与选型说明

| 组件 | 技术 | 选型原因 |
|---|---|---|
| Web 框架 | FastAPI | 原生异步，类型注解自动生成 OpenAPI 文档，部署简单（单进程 uvicorn） |
| 知识数据库 | PostgreSQL (telecom_kb) | 事务保证 + pgvector 扩展 + JSONB + 数组类型，一库解决关系/向量/文档三类需求 |
| 爬虫数据库 | PostgreSQL (telecom_crawler) | 与知识库物理隔离，独立 connection pool，故障不传播 |
| 图数据库 | Neo4j Community | 原生图遍历；MERGE 语义天然幂等；动态关系类型是一等公民 |
| 对象存储 | MinIO | S3 兼容 API，自部署，HTML/文本大文件不占 PG |
| Embedding | Ollama (bge-m3 1024维) | 本地推理，零 API 成本，中英双语，无 Python 额外依赖 |
| LLM | OpenAI 兼容 API (DeepSeek/Claude) | 关系抽取、候选词分类；通过 base-url + api-key 切换厂商 |
| 框架抽象 | semcore（自研） | 零依赖 ABC 层；Pipeline Stage、Operator、Store、ConflictDetector 可独立测试 |

---

## 2. 工程目录结构

```
Self_Knowledge_Evolve/
├── semcore/                       ← 零依赖框架抽象层（可独立发布为包）
│   └── semcore/
│       ├── core/
│       │   ├── context.py         ← PipelineContext（管线执行状态容器）
│       │   └── types.py           ← Fact / Segment / OntologyNode 数据类
│       ├── pipeline/
│       │   └── base.py            ← Stage ABC（process 方法签名）
│       ├── operators/
│       │   └── base.py            ← SemanticOperator ABC
│       ├── providers/
│       │   └── base.py            ← RelationalStore / GraphStore / ObjectStore ABC
│       └── governance/
│           └── base.py            ← ConflictDetector / EvolutionGate ABC
│
├── src/
│   ├── app.py                     ← FastAPI 应用入口，SemanticApp 单例组装
│   ├── app_factory.py             ← build_app() 工厂，依赖注入主入口
│   │
│   ├── api/
│   │   ├── semantic/
│   │   │   ├── router.py          ← 21 个语义算子 REST 端点注册
│   │   │   ├── lookup.py          ← lookup 业务逻辑
│   │   │   ├── expand.py          ← expand 业务逻辑
│   │   │   ├── path.py            ← path 业务逻辑
│   │   │   ├── dependency.py      ← dependency_closure 业务逻辑
│   │   │   ├── impact.py          ← impact_propagate 业务逻辑
│   │   │   ├── filter.py          ← filter 业务逻辑
│   │   │   ├── evidence.py        ← evidence_rank / conflict_detect / fact_merge
│   │   │   ├── evolution.py       ← candidate_discover / attach_score / evolution_gate
│   │   │   ├── context_assemble.py← context_assemble 业务逻辑
│   │   │   ├── ontology_quality.py← ontology_quality 业务逻辑
│   │   │   └── stale_knowledge.py ← stale_knowledge 业务逻辑
│   │   └── system/
│   │       ├── router.py          ← 系统管理端点注册
│   │       ├── stats.py           ← 监控快照端点
│   │       ├── review.py          ← 候选词审核（approve/reject/merge/check_synonyms）
│   │       └── drilldown.py       ← 质量指标钻取
│   │
│   ├── pipeline/
│   │   ├── pipeline_factory.py    ← 组装 7 阶段 Pipeline
│   │   └── stages/
│   │       ├── stage1_ingest.py   ← IngestStage
│   │       ├── stage2_segment.py  ← SegmentStage
│   │       ├── stage3_align.py    ← AlignStage（规则 A1-A5）
│   │       ├── stage3b_evolve.py  ← EvolveStage（五维评分 + 六道门控）
│   │       ├── stage4_extract.py  ← ExtractStage（规则 R1-R4 + LLM）
│   │       ├── stage5_dedup.py    ← DedupStage（规则 D1-D5）
│   │       └── stage6_index.py    ← IndexStage（Neo4j MERGE）
│   │
│   ├── governance/
│   │   ├── conflict_detector.py   ← TelecomConflictDetector
│   │   └── maintenance.py         ← OntologyMaintenance（三段式维护）
│   │
│   ├── ontology/
│   │   ├── registry.py            ← OntologyRegistry（内存缓存）
│   │   ├── validator.py           ← 完整性校验
│   │   └── yaml_provider.py       ← YAML 读写封装
│   │
│   ├── stats/
│   │   ├── collector.py           ← StatsCollector（7 类指标）
│   │   ├── scheduler.py           ← StatsScheduler（定时触发）
│   │   ├── ontology_quality.py    ← OntologyQualityCalculator（5 维度 20 指标）
│   │   ├── drilldown.py           ← 21 种钻取指标路由
│   │   └── backfill.py            ← BackfillWorker（审批后回填 segment_tags）
│   │
│   ├── operators/
│   │   ├── __init__.py            ← ALL_OPERATORS 列表
│   │   ├── lookup_op.py
│   │   ├── expand_op.py
│   │   ├── path_op.py
│   │   └── ... (21 个 Operator)
│   │
│   ├── providers/
│   │   ├── postgres_store.py      ← PostgreSQL RelationalStore (telecom_kb)
│   │   ├── crawler_postgres_store.py ← PostgreSQL RelationalStore (telecom_crawler)
│   │   ├── neo4j_store.py         ← Neo4j GraphStore
│   │   ├── minio_store.py         ← MinIO ObjectStore
│   │   └── anthropic_llm.py       ← LLM Provider（OpenAI 兼容）
│   │
│   ├── config/
│   │   └── settings.py            ← Pydantic Settings（环境变量绑定）
│   │
│   ├── dev/                       ← 开发模式替代实现（无外部服务）
│   │   ├── fake_postgres.py       ← SQLite :memory: 替代 PostgreSQL
│   │   ├── fake_crawler_postgres.py
│   │   ├── fake_neo4j.py          ← dict 图替代 Neo4j
│   │   └── seed.py                ← 从 YAML 初始化开发数据
│   │
│   └── utils/
│       ├── embedding.py           ← Embedding 客户端（Ollama 优先 → sentence-transformers 兜底）
│       ├── llm_extract.py         ← LLMExtractor（关系抽取 + RST + 候选词分类）
│       ├── normalize.py           ← normalize_term（词边界保留归一化）
│       ├── confidence.py          ← score_fact（五维置信度公式）
│       ├── hashing.py             ← SimHash + Jaccard + hamming_distance
│       └── text.py                ← normalize_text（去重用简化归一化）
│
├── ontology/
│   ├── domains/
│   │   ├── ip_network.yaml        ← 概念层（74 节点）
│   │   ├── ip_network_mechanisms.yaml ← 机制层（24 节点）
│   │   ├── ip_network_methods.yaml    ← 方法层（22 节点）
│   │   ├── ip_network_conditions.yaml ← 条件层（20 节点）
│   │   ├── ip_network_scenarios.yaml  ← 场景层（13 节点）
│   │   └── ip_network_evolved.yaml    ← 审批通过的演化节点
│   ├── top/
│   │   └── relations.yaml         ← 71 种关系类型定义
│   ├── lexicon/
│   │   └── aliases.yaml           ← 871 条别名（中英文 + 厂商变体）
│   ├── patterns/
│   │   ├── semantic_roles.yaml    ← 22 种语义角色匹配模式
│   │   ├── context_signals.yaml   ← 6 种上下文信号
│   │   ├── predicate_signals.yaml ← 13 种谓语信号
│   │   └── candidate_stopwords.yaml ← 候选词停用词表
│   ├── seeds/
│   │   ├── cross_layer_relations.yaml ← 56 条跨层种子关系
│   │   ├── axiom_relations.yaml       ← 48 条公理关系
│   │   └── classification_fixes.yaml  ← 3 条分类修正
│   └── governance/
│       └── evolution_policy.yaml  ← 评分权重、门控阈值
│
├── scripts/
│   ├── reset_and_run.py           ← 原子化重置 + 启动（开发调试）
│   ├── load_ontology.py           ← YAML → Neo4j + PG 同步（冷启动）
│   ├── clean_candidates.py        ← 手动触发 OntologyMaintenance
│   ├── export_dashboard.py        ← Dashboard 导出为离线单文件 HTML
│   └── migrate_normalized_forms.py ← 一次性迁移旧归一化格式
│
├── static/
│   └── dashboard.html             ← 5 Tab 单页应用（Chart.js）
│
├── worker.py                      ← 4 线程守护进程入口
├── run_dev.py                     ← 开发模式启动（内存存储 + 自动 seed）
└── requirements.txt
```

---

## 3. semcore 框架抽象层

semcore 是本系统的零依赖框架，与业务代码完全解耦。它只定义抽象基类（ABC），不包含任何 PostgreSQL/Neo4j/MinIO 的实现代码。

### 3.1 为什么需要这层

在没有 semcore 的情况下，Stage 代码会直接引用 `psycopg2.connect()` 或 `neo4j.GraphDatabase.driver()`，导致：
- 单元测试必须启动真实数据库
- 开发模式需要所有外部服务

semcore 的 ABC 让 Stage 代码依赖接口而不是实现，开发模式可以注入 `FakePostgres`（SQLite :memory:）和 `FakeNeo4j`（dict），单元测试零外部依赖。

### 3.2 核心接口

```python
# semcore/providers/base.py

class RelationalStore(ABC):
    def fetchall(self, sql: str, params=()) -> list[dict]: ...
    def fetchone(self, sql: str, params=()) -> dict | None: ...
    def execute(self, sql: str, params=()) -> None: ...

class GraphStore(ABC):
    def read(self, cypher: str, params: dict = {}) -> list[dict]: ...
    def write(self, cypher: str, params: dict = {}) -> None: ...

class ObjectStore(ABC):
    def upload(self, key: str, data: bytes, content_type: str) -> str: ...
    def download(self, key: str) -> bytes: ...

# semcore/pipeline/base.py
class Stage(ABC):
    name: str                                # 子类必须定义
    def process(self, ctx: PipelineContext, app) -> PipelineContext: ...

# semcore/operators/base.py
class SemanticOperator(ABC):
    name: str
    def execute(self, **kwargs) -> dict: ...
```

### 3.3 开发模式替代实现

`src/dev/` 目录包含所有外部服务的内存替代：

- `FakePostgres`（fake_postgres.py）：用 SQLite `:memory:` 实现 RelationalStore，启动时从 `seed.py` 初始化表结构和本体数据。注意：SQLite 不支持 `governance.` schema 前缀，FakePostgres 会自动剥离前缀（`governance.evolution_candidates` → `evolution_candidates`）。
- `FakeNeo4j`（fake_neo4j.py）：用 Python dict 实现 GraphStore，仅支持基本的 MERGE/MATCH Cypher，不支持复杂路径查询。
- 开发模式启动：`python run_dev.py`，所有 Stage 和 Operator 注入 fake 实现，不需要任何外部服务。

---

## 4. 配置管理

所有配置通过 `src/config/settings.py` 中的 Pydantic Settings 绑定，支持 `.env` 文件和环境变量覆盖。

```python
class Settings(BaseSettings):
    # PostgreSQL
    postgres_dsn: str = "postgresql://postgres:postgres@127.0.0.1:5432/telecom_kb"
    crawler_postgres_dsn: str = "postgresql://postgres:postgres@127.0.0.1:5432/telecom_crawler"

    # Neo4j
    NEO4J_URI: str = "bolt://127.0.0.1:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "password"
    NEO4J_DATABASE: str = "neo4j"

    # MinIO
    MINIO_ENDPOINT: str = "127.0.0.1:9001"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET_RAW: str = "telecom-kb-raw"
    MINIO_BUCKET_CLEANED: str = "telecom-kb-cleaned"

    # LLM
    LLM_ENABLED: bool = True
    LLM_API_KEY: str = ""
    LLM_API_BASE: str = "https://api.deepseek.com/v1"
    LLM_MODEL: str = "deepseek-chat"

    # Embedding
    EMBEDDING_ENABLED: bool = True
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_EMBED_MODEL: str = "bge-m3"

    # Worker
    WORKER_PIPELINE_LIMIT: int = 5      # 每轮处理的最大文档数
    WORKER_SLEEP_SECS: int = 5          # 空闲轮询间隔
    STARTUP_HEALTH_REQUIRED: bool = True # 启动时检查 LLM 可用性
```

**重要**：`STARTUP_HEALTH_REQUIRED=True` 时，`worker.py` 启动时会 ping LLM API，不可用则 `sys.exit(1)`，应用不启动。这是刻意设计，不是 bug。

---

## 5. Pipeline 七阶段详细规格

### 5.1 PipelineContext — 管线状态传递

```python
@dataclass
class PipelineContext:
    source_doc_id: str
    doc: Document | None = None
    segments: list[Segment] = field(default_factory=list)
    facts: list[Fact] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stage_outputs: dict[str, Any] = field(default_factory=dict)
```

每个 Stage 通过 `self.set_output(ctx, {"key": value})` 把结果写入 `ctx.stage_outputs`，下游 Stage 可以读取。Stage 之间不共享内存对象，全部通过数据库中转（便于重跑和断点恢复）。

### 5.2 Stage 1 — IngestStage

**文件**：`src/pipeline/stages/stage1_ingest.py`

**职责**：从 MinIO 加载原始 HTML → 清洗 → 质量门控 → 写 cleaned/ → 更新 documents 状态

**详细流程**：

```
1. documents 表查 source_doc_id，取 raw_storage_uri
2. MinIO.download(raw_storage_uri) → raw_html bytes
3. 内容哈希去重：SHA-256(raw_html) → content_hash
   SELECT COUNT(*) FROM documents WHERE content_hash=%s AND source_doc_id!=%s
   如已存在 → status='duplicate'，跳过
4. HTML 清洗：
   a. BeautifulSoup 提取正文（去除 script/style/nav/footer）
   b. 去除多余空行、HTML 实体
   c. 中文分词预处理（插入空格，方便后续 token 化）
5. 质量门控：
   len(clean_text) < 200 → status='low_quality'，跳过
   非 ASCII 比率 > 0.9 → status='low_quality'（可能是图片转文字的乱码）
6. doc_type 检测（纯 URL 规则，不调 LLM）：
   datatracker.ietf.org / rfc\d+ → 'standard'
   draft-* → 'draft'
   cisco.com / huawei.com / juniper.net / arista.com → 'vendor_doc'
   else → 'tech_article'
7. MinIO.upload("telecom-kb-cleaned", "{content_hash}.txt", clean_text)
8. UPDATE documents SET status='cleaned', cleaned_storage_uri=..., doc_type=..., content_hash=...
```

**已知约束**：
- 原始 HTML 存放在 `telecom-kb-raw/`，路径格式为 `{site_key}/{crawl_date}/{url_hash}.html`
- 清洗后文本路径为 `telecom-kb-cleaned/{content_hash}.txt`，相同内容只存一份
- 失败时 status 必须更新（不能留 raw），否则下次 pipeline 会重复处理

### 5.3 Stage 2 — SegmentStage

**文件**：`src/pipeline/stages/stage2_segment.py`

**职责**：将 clean_text 切分为段落，并为每个段落计算 SimHash、RST 关系、语义角色

**切分策略（按 doc_type 选择）**：

```python
if doc_type == 'standard':
    # RFC 格式：按数字标题切（"1.  Introduction"）
    _split_rfc(text)
elif 检测到 markdown heading (# ## ###):
    _split_markdown(text)
else:
    # 通用：按双空行切段，超长段落按句子滑动窗口再切
    _split_generic(text)
```

**每个段落的处理**：

1. **SimHash**（`src/utils/hashing.py`）：
   - 对 clean_text 做词频加权的位向量叠加，取符号得 64 位整数
   - 存入 `segments.simhash_value`（bigint）
   - 后续 Stage 5 用 Hamming 距离判断近重复

2. **RST 篇章关系**（相邻段落对）：
   - 取相邻两个段落（EDU-A, EDU-B），调 LLM 判断 RST 关系类型
   - 21 种 RST 类型（见第 9 节）
   - 存入 `t_rst_relation` 表（`src_segment_id`, `dst_segment_id`, `relation_type`）
   - Stage 4 用这个信息决定是否合并相邻段落重试 LLM 抽取

3. **语义角色分类**（无 LLM，纯规则）：
   从 `ontology/patterns/semantic_roles.yaml` 加载模式，匹配后打 segment_type：

   | segment_type | 典型特征 |
   |---|---|
   | definition | "is defined as", "refers to", "known as", "称为" |
   | mechanism | 包含机制层关键词（协议报文格式、算法步骤） |
   | constraint | "MUST", "SHALL", "REQUIRED", "不得" |
   | config | CLI 命令格式（含 `router#`、`interface`、`<>`） |
   | fault | "error", "failure", "alarm", "故障" |
   | troubleshooting | "troubleshoot", "diagnose", "排障", "check" |
   | best_practice | "best practice", "recommended", "建议" |
   | performance | "throughput", "latency", "QoS", "性能" |
   | comparison | "compared to", "versus", "vs", "相比" |
   | table | 包含 `|` 格式的表格 |
   | code | 缩进 4 空格或包含 ` ``` ` 的代码块 |

**输出**：`segments` 表行，每行含 `segment_id`、`source_doc_id`、`segment_index`、`segment_type`、`raw_text`、`normalized_text`（小写去符号）、`simhash_value`、`token_count`

### 5.4 Stage 3 — AlignStage

**文件**：`src/pipeline/stages/stage3_align.py`

**职责**：把段落文本和本体节点对齐，产生 segment_tags；发现候选词

**规则 A1-A5 执行顺序**：

**A1 — 精确别名匹配**（主路径）

```python
def _find_terms(self, text: str) -> list[tuple[str, str, float]]:
    text_lower = text.lower()
    for surface, node_id in ontology.alias_map.items():
        if len(surface) <= 3:
            # 短术语（IP, TCP, BGP）必须有词边界，防止 "sp" 匹配 "specification" 内部
            if not re.search(r"\b" + re.escape(surface) + r"\b", text_lower):
                continue
        else:
            # 长术语直接 contains 检查（更快）
            if surface not in text_lower:
                continue

        # 精确匹配 canonical_name → confidence=1.0，别名 → confidence=0.90
        conf = 1.0 if node.canonical_name.lower() == surface else 0.90
        found.append((surface, node_id, conf))
```

**短术语词边界的重要性**：`\b` 使用是为了解决一个实际问题——"IS" 是 Intermediate System（IS-IS 协议的核心概念），但它也出现在 "This IS the configuration" 中。不加词边界会产生大量假阳性。

**A2 — Embedding 兜底**（仅当精确匹配产生 0 个 canonical 标签时触发）

```python
if canonical_count == 0:
    # 用段落文本（截断至 512 tokens）对比所有本体节点 embedding
    # 本体节点 embedding 在首次调用时懒加载并缓存到类变量（_onto_embeddings）
    # cosine > 0.80 且 top-3 命中 → 打 embedding 标签（tagger='embedding'）
    for node_id, conf in self._embedding_match(text[:512]):
        ...
```

**为什么不是所有段落都跑 Embedding**：embedding 匹配比精确匹配慢 100 倍，全部段落跑会让 pipeline 吞吐降低一个数量级。只在精确匹配完全失败时触发，是性能和召回的平衡点。

**A3 — 语义角色标签**

```python
if seg_type in _SEMANTIC_ROLE_TAGS:
    tags.append({"tag_type": "semantic_role", "tag_value": 对应中文标签, "confidence": 1.0})
```

**A4 — 上下文信号标签**

从 `ontology/patterns/context_signals.yaml` 加载模式，匹配前 1000 个字符（标题区域，信号密度高）：
```python
for pattern, ctx in self._context_patterns:
    if pattern.search(text[:1000]):
        tags.append({"tag_type": "context", "tag_value": ctx, "confidence": 0.85})
```

**A5 — 候选词发现**

`_collect_candidates()` 从 raw_text（保留大小写）中提取潜在新术语：
- 通过 LLM（`LLMExtractor.classify_candidate_term()`）判断是否是新概念
- 归一化：`normalize_term(term)`（见第 7 节）
- 去重：normalized_form 在 `governance.evolution_candidates` 里 UNIQUE 约束
- 写入时记录 `source_doc_id`、`segment_id` 作为例证（`examples` JSONB 字段）

**segment_tags 写入**：每个 `(segment_id, ontology_node_id)` 对通过 `INSERT ... ON CONFLICT DO NOTHING` 保持幂等。

**状态更新**：如果对齐后 canonical 标签数量 = 0，segments 状态更新为 `pending_alignment`（标记为低质量，不进入 Stage 4）。

### 5.5 Stage 4 — ExtractStage

**文件**：`src/pipeline/stages/stage4_extract.py`

**职责**：从段落文本中抽取 (subject, predicate, object) 三元组，写入 facts + evidence 表

Stage 4 有**三条路径**，按优先级顺序尝试，找到结果就跳过后续路径：

**路径 1：LLM 直接抽取**

```python
facts = extract_facts_llm(seg, source_rank)
if facts:
    all_facts.extend(facts)
    continue
```

LLM 调用细节：
- 提示词（系统 prompt）包含本体节点 ID 列表和有效谓语列表
- 要求只使用节点 ID 作为 subject/object（不接受自由文本实体）
- **关键设计**：允许 LLM 创造不在列表中的新谓语——如果文本的语义无法用已有谓语表达，LLM 应该创造一个新谓语（`lowercase_with_underscores`），这条关系会作为 `candidate_type='relation'` 进入候选词池等待审核

```python
# src/utils/llm_extract.py 中的系统 prompt 关键句
# "if the text clearly expresses a relationship that does NOT fit any predicate,
#  you MUST create a new predicate name (e.g. 'replaces', 'supersedes').
#  Do NOT force-fit into an existing predicate when the semantics don't match."
```

**路径 2：合并上下文重试**

```python
if i > 0:
    # 检查当前段落与前一段落的 RST 关系
    # 只有 continuative 类型（Elaboration/Sequence/Restatement/Explanation）才合并
    merged_facts = _extract_merged_context(segments[i-1], seg, source_rank, source_doc_id)
    if merged_facts:
        all_facts.extend(merged_facts)
        continue
```

**为什么需要合并上下文**：很多技术文档把一个事实分散在两个相邻段落里——第一段介绍概念（"OSPF 使用 SPF 算法"），第二段补充细节（"SPF 算法基于 Dijkstra 最短路径"）。单独发送每个段落，LLM 可能因为信息不完整而无法抽取有效三元组。合并后变成一个完整的语境，抽取成功率显著提升。

**路径 3：共现兜底**

```python
cooc_facts = _extract_cooccurrence(seg, source_rank)
```

触发条件：该段落有 canonical 标签（即已知本体节点出现在段落里）。
限制：只在同一段落里出现的节点对之间产生 `co_occurs_with` 关系，最多 1 条。
原因：共现关系没有语义方向性，产生太多是噪声。限制为最多 1 条是为了保证每个段落都有最基本的图连接，不是为了大量积累。

**置信度计算**（`src/utils/confidence.py`）：

```python
def score_fact(source_rank, extraction_method, ontology_fit=0.8,
               cross_source_consistency=0.5, temporal_validity=1.0) -> float:
    sa = {'S': 1.0, 'A': 0.85, 'B': 0.65, 'C': 0.40}[source_rank]
    em = {'manual': 1.0, 'rule': 0.85, 'llm': 0.70}[extraction_method]
    return (0.30 * sa + 0.20 * em + 0.20 * ontology_fit
          + 0.20 * cross_source_consistency + 0.10 * temporal_validity)
```

注意：`extraction_method='rule'` 分值（0.85）高于 `'llm'`（0.70），因为精心设计的规则对电信领域的特定模式准确率更稳定；LLM 虽然覆盖范围广，但在边界案例上的不确定性更高。

### 5.6 Stage 5 — DedupStage

**文件**：`src/pipeline/stages/stage5_dedup.py`

**规则 D2 — 段落级 SimHash 去重**

```python
SIMHASH_NEAR_DUP_THRESHOLD = 3   # Hamming 距离 ≤ 3
JACCARD_DUP_THRESHOLD = 0.85     # 需要二次 Jaccard 确认

for i, a in enumerate(segments):
    for j in range(i+1, len(segments)):
        b = segments[j]
        hd = hamming_distance(a.simhash_value, b.simhash_value)
        if hd <= SIMHASH_NEAR_DUP_THRESHOLD:
            # SimHash 接近时，用 Jaccard 二次确认（避免 SimHash 误判）
            if jaccard_similarity(normalize_text(a.raw_text),
                                  normalize_text(b.raw_text)) >= JACCARD_DUP_THRESHOLD:
                # 标记较晚出现的段落为 superseded（保留原始顺序中的第一个）
                UPDATE segments SET lifecycle_state='superseded' WHERE segment_id=b.segment_id
```

**为什么 SimHash 之后还需要 Jaccard 确认**：SimHash 是近似算法，存在少量误判（两个不相关的段落恰好 Hamming 距离 ≤ 3）。Jaccard 是精确的 token 级别集合相似度，用于排除 SimHash 误判。两者结合：SimHash 快速筛选候选对（O(n)），Jaccard 精确确认（只对少量候选对执行）。

**规则 D3-D5 — 事实去重与冲突检测**

```
D3 — 精确去重：(subject, predicate, object) 完全相同 → 保留最高置信度，增加 source_count
D4 — 语义去重：subject embedding 相似 + object embedding 相似 + predicate 相同 → 合并
D5 — 冲突检测：调用 TelecomConflictDetector.detect()（见下）
```

**TelecomConflictDetector 的两个检测信号**（`src/governance/conflict_detector.py`）：

```python
# 信号 1：精确冲突（S+P 相同，O 不同）
SELECT fact_id FROM facts WHERE subject=%s AND predicate=%s AND object!=%s AND lifecycle_state='active'

# 信号 2：语义冲突（S≈S AND O≈O，但 P 不同）
# 用 embedding 对比 "subject object" 文本对
# 相似度高但 predicate 不同 → 可能是同一关系的不同表述（或真实冲突）
my_text = f"{fact.subject} {fact.object}".lower()
# 与当前 facts 表中谓语不同的 facts 对比，取 top-50
# cosine > 阈值 → 标记为 semantic_conflict
```

冲突写入 `governance.conflict_records`，不自动解决，等待人工或 Maintenance 处理。

### 5.7 Stage 6 — IndexStage

**文件**：`src/pipeline/stages/stage6_index.py`

**职责**：将通过质量门控的 fact 写入 Neo4j 图

**置信度门控**：`fact.confidence >= INDEX_THRESHOLD`（阈值从 `evolution_policy.yaml` 读取，默认 0.35）

**Neo4j MERGE 模式**（幂等写入）：

```cypher
// 关系节点 MERGE（主键：source + type + target）
MERGE (src {node_id: $src_node_id})
MERGE (dst {node_id: $dst_node_id})
MERGE (src)-[r:RELATION_TYPE {src: $src_node_id, type: $predicate, dst: $dst_node_id}]->(dst)
ON CREATE SET r.fact_count = 1, r.confidence_sum = $confidence, ...
ON MATCH SET r.fact_count = r.fact_count + 1, r.confidence_sum = r.confidence_sum + $confidence

// 关系类型转换：predicate → UPPER_SNAKE（Neo4j 关系类型规范）
# "depends_on" → "DEPENDS_ON"
# "uses_protocol" → "USES_PROTOCOL"
```

**动态关系类型的安全处理**：predicate 字段来自 LLM，存在注入风险。转换规则：
```python
rel_type = re.sub(r"[^A-Z0-9_]", "_", predicate.upper())
if not re.match(r"^[A-Z][A-Z0-9_]*$", rel_type):
    rel_type = "UNKNOWN_RELATION"  # 不符合命名规则的 fallback
```

**文档状态更新**：Stage 6 完成后 `documents.status = 'indexed'`，这是最终状态。

---

## 6. 候选词治理详细规格

### 6.1 候选词数据模型

```sql
-- governance.evolution_candidates
candidate_id        UUID PRIMARY KEY
normalized_form     VARCHAR UNIQUE  -- normalize_term() 的输出
surface_forms       TEXT[]          -- 所有见过的原始表面形式
candidate_type      VARCHAR         -- 'concept' | 'relation'
source_count        INT             -- 多少篇文档包含此术语
examples            JSONB           -- [{text, segment_id, doc_id}, ...]（最多 5 个）
candidate_parent_id VARCHAR         -- 如果是已有节点的子节点，指向父节点 ID
review_status       VARCHAR         -- discovered / pending / pending_review / accepted / rejected / noise_deleted / variant_merged
-- 五维评分
source_diversity_score   FLOAT
temporal_stability_score FLOAT
structural_fit_score     FLOAT
retrieval_gain_score     FLOAT
synonym_risk_score       FLOAT
composite_score     FLOAT
-- 时间戳
first_seen_at, last_seen_at, accepted_at, created_at
reviewer, review_note
```

### 6.2 候选词归一化（normalize_term）

这是候选词管理中最关键的函数，保证同一个概念的不同表面形式（"BGP-4"、"BGP4"、"BGP v4"）最终归一到相同的 key，不产生重复候选词。

**规则（按执行顺序）**：
1. 去括号内容：`"network layer reachability information (NLRI)"` → `"network layer reachability information"`
2. 去首位冠词：`"the BGP protocol"` → `"BGP protocol"`
3. 连字符处理：
   - 缩写对（匹配 `^[A-Z][A-Z0-9]{0,5}-[A-Z][A-Z0-9]{0,5}$`）保留连字符：`MPLS-TE` → `mpls-te`，`IS-IS` → `is-is`
   - 其他连字符替换为空格：`Router-ID` → `router id`
4. 版本号拆分：`BGPv4` → `bgp 4`，`v4` → `4`（消除版本差异）
5. 复数还原（仅非全大写 token）：
   - `policies` → `policy`，`routers` → `router`
   - 保护词表（`dns`, `qos`, `is`, `as` 等）不被误还原

**设计约束**：输出必须是空格分隔的 token，不是驼峰或连接形式。原因：
- 支持后续 Jaccard 相似度计算（按 token 集合对比）
- 支持包含关系检测（`bgp` ⊂ `bgp protocol`）
- 保持人类可读性

### 6.3 五维评分与六道门控

**Stage 3b EvolveStage** 计算每个候选词的评分并决定状态流转：

```python
composite_score = (
    w_sd × source_diversity_score +     # 来源多样性（不同站点数量）
    w_ts × temporal_stability_score +   # 时间稳定性（首次与最后见到的时间跨度）
    w_sf × structural_fit_score +       # 结构适配性（和已有本体节点的层级兼容度）
    w_rg × retrieval_gain_score +       # 检索增益（加入后能提升多少段落的命中率）
    w_sr × (1 - synonym_risk_score)     # 同义词风险（越低越好）
)
```

权重从 `ontology/governance/evolution_policy.yaml` 读取，默认：`0.25 / 0.20 / 0.25 / 0.15 / 0.15`

**六道门控**：
```python
gates = {
    "source_count_gate":    candidate.source_count >= MIN_SOURCE_COUNT,  # 默认 2
    "diversity_gate":       candidate.source_diversity_score >= MIN_DIVERSITY,
    "stability_gate":       candidate.temporal_stability_score >= MIN_STABILITY,
    "fit_gate":             candidate.structural_fit_score >= MIN_FIT,
    "synonym_risk_gate":    candidate.synonym_risk_score < MAX_SYNONYM_RISK,
    "composite_gate":       candidate.composite_score >= AUTO_ACCEPT_THRESHOLD,  # 默认 0.7
}
passed = all(gates.values())
```

所有六道门都过 → `status='pending_review'`（送人工审核）
分数低于 `AUTO_REJECT_THRESHOLD`（默认 0.3）→ `status='rejected'`（自动拒绝）

### 6.4 OntologyMaintenance — 三段式维护

**Pass 1：Embedding 聚类去重**

```python
# 对所有 discovered/pending 状态候选词计算 embedding
# 构建相似度矩阵（numpy 向量化，O(n²)）
MERGE_THRESHOLD = 0.85   # cosine 相似度
for i, j 组合:
    if sim(candidates[i], candidates[j]) >= MERGE_THRESHOLD:
        # 保留 source_count 更大的，其 surface_forms 合并另一个
        # 另一个标记 status='variant_merged'
```

**Pass 2：LLM 批量分类**

对 Pass 1 后剩余的未决候选词，每批 20 个发给 LLM：

```python
# LLM 对每个候选词返回：
# - "new_concept": 确实是本体中没有的新概念 → 保留，待人工审核
# - "variant": 是已有本体节点的变体/同义词 → 合并知识到原有节点
# - "noise": 错误提取，不是有意义的术语 → 删除
```

**Pass 3：清理执行**

```python
# noise → DELETE FROM governance.evolution_candidates
# variant → 执行知识合并：
#   1. 找到原有本体节点（LLM 返回的 parent_concept）
#   2. candidate.examples 里的 segment_id → 给对应节点补 canonical tag
#   3. candidate.surface_forms → 追加为节点别名（lexicon_aliases + alias_map）
#   4. status='variant_merged'
```

**知识不丢失原则**：Pass 3 的知识合并保证了候选词即使被"删除"，它出现过的文档段落仍然通过别名扩展被关联到正确的本体节点。

### 6.5 approve_candidate — 审批写入

当人工调用 `POST /api/v1/system/review/{id}/approve` 时：

**概念型候选词（candidate_type='concept'）**：
```python
def _approve_concept(candidate, ...):
    node_id = f"IP.{normalized_form.upper().replace(' ', '_')}"  # 生成节点 ID
    # 1. Neo4j：MERGE (n:OntologyNode {node_id}) SET 所有属性
    #           MERGE (n)-[:SUBCLASS_OF]->(parent_node)  如果指定了 parent_node_id
    #           MERGE (n)-[:ALIAS_OF]->() 为每个 surface_form
    # 2. PG：INSERT INTO lexicon_aliases
    # 3. YAML：追加到 ontology/domains/ip_network_evolved.yaml
    # 4. YAML：别名追加到 ontology/lexicon/aliases.yaml
    # 5. Git：commit "Ontology v{version}: Approved concept: {normalized_form}"
    # 6. BackfillWorker.schedule()：回填已有 segments 的 segment_tags
```

**关系型候选词（candidate_type='relation'）**：
```python
def _approve_relation(candidate, ...):
    # 1. Neo4j：写入关系类型元节点（RelationType 节点）
    # 2. YAML：追加到 ontology/top/relations.yaml（category='evolved'）
    # 3. Git：commit
```

**版本号更新**：每次 approve 调用 `_bump_version()` 在 `governance.ontology_versions` 里插入新版本行（格式 `v0.1.XX`），并通知 OntologyRegistry 刷新 `current_version`。

---

## 7. 本体 YAML 结构

### 7.1 节点定义（五个主要文件 + 1 个演化文件）

```yaml
# ontology/domains/ip_network.yaml
nodes:
  - id: IP.BGP
    canonical_name: BGP
    display_name_zh: 边界网关协议
    knowledge_layer: concept           # concept / mechanism / method / condition / scenario
    parent_id: IP.ROUTING_PROTOCOL     # 层级父节点
    maturity_level: core               # core / standard / extended
    description: "Path-vector EGP for inter-AS routing. RFC 4271."
    aliases:
      - Border Gateway Protocol
      - BGP4
      - BGP-4
      - 边界网关协议
      - 边界网关协议（BGP）
    allowed_relations: [uses_protocol, establishes, advertises, depends_on]
    source_basis: [IETF]
    lifecycle_state: active            # active / deprecated / experimental
```

### 7.2 关系类型定义

```yaml
# ontology/top/relations.yaml
relations:
  - id: depends_on
    category: dependency               # dependency / operational / functional / structural / evolved
    description: "A requires B to function correctly"
    domain_hint: concept|mechanism
    range_hint: concept
    symmetric: false
    transitive: true                   # 传递性：BGP depends_on TCP, TCP depends_on IP → BGP depends_on IP
```

### 7.3 别名词典

```yaml
# ontology/lexicon/aliases.yaml（871 条）
aliases:
  - surface: "Border Gateway Protocol"
    node_id: IP.BGP
    alias_type: formal                 # formal / abbreviation / vendor_variant / chinese / evolved
    language: en

  - surface: "边界网关协议"
    node_id: IP.BGP
    alias_type: chinese
    language: zh
```

`alias_type: evolved` 标记了审批通过后自动追加的别名，便于区分人工维护和自动学习的内容。

### 7.4 模式外部化（patterns/）

所有正则匹配模式存放在 YAML，不硬编码在 Python 代码里：

```yaml
# ontology/patterns/semantic_roles.yaml
roles:
  - name: definition
    patterns:
      - "(?i)(is defined as|refers to|known as|称为|是指)"
      - "(?i)(definition:|Definition:)"
    confidence: 0.95
```

OntologyRegistry 启动时加载这些模式并编译为 `re.Pattern` 对象。修改匹配规则只需修改 YAML，不改代码，也不需要重启（通过 `POST /reload_ontology` 热重载）。

---

## 8. 21 个语义算子规格

所有算子的响应格式：
```json
{
  "meta": {"ontology_version": "v0.1.63", "latency_ms": 42},
  "result": { ...算子特定数据... }
}
```

| # | 端点 | 方法 | 核心逻辑 | 关键参数 |
|---|---|---|---|---|
| 1 | `/lookup` | GET | 别名 → 节点 ID → 返回节点元数据 + 所有别名 | `term` |
| 2 | `/resolve` | GET | 别名 → canonical_name + 所有 surface_forms | `alias` |
| 3 | `/expand` | GET | Neo4j BFS，返回 N 跳内所有节点和边 | `node_id`, `hops=1` |
| 4 | `/path` | GET | Neo4j shortestPath，返回路径节点和边序列 | `from`, `to` |
| 5 | `/dependency_closure` | GET | 沿 DEPENDS_ON/REQUIRES 边递归遍历，返回传递闭包 | `node_id` |
| 6 | `/impact_propagate` | POST | 反向遍历，找出所有依赖于目标节点的组件 | `node_id` |
| 7 | `/filter` | GET | 过滤本体节点（domain/maturity/lifecycle） | `domain`, `maturity_level`, `lifecycle_state` |
| 8 | `/evidence_rank` | GET | 加载 fact + evidence，按 confidence DESC 排序 | `node_id` |
| 9 | `/conflict_detect` | POST | 查 conflict_records + 实时 embedding 检测 | `node_id` |
| 10 | `/fact_merge` | POST | 同 S+P 合并 fact，保留最高置信度，删除其余 | `subject`, `predicate` |
| 11 | `/candidate_discover` | POST | 分页查询 evolution_candidates | `status`, `limit`, `offset` |
| 12 | `/attach_score` | POST | 重算 candidate 五维分数并写回 | `candidate_id` |
| 13 | `/evolution_gate` | POST | 执行六道门控，决定是否晋升候选词状态 | `candidate_id` |
| 14 | `/context_assemble` | POST | 展开多个节点的邻居 + fact + segment，组装 LLM 上下文包 | `node_ids`, `max_segments` |
| 15 | `/semantic_search` | POST | query → embedding → pgvector cosine 搜索 segments | `query`, `limit=10` |
| 16 | `/ontology_quality` | GET | 调用 OntologyQualityCalculator.compute_all() | — |
| 17 | `/stale_knowledge` | GET | 找 confidence 低且长期无新 evidence 的 fact | `days`, `threshold` |
| 18 | `/edu_search` | GET | semantic_search 过滤 definition/best_practice 类型 | `query` |
| 19 | `/knowledge_gap` | GET | 找本体节点覆盖率（有 fact/无 fact）、知识空洞 | — |
| 20 | `/subgraph` | GET | 提取 N 跳子图（节点+边），用于可视化 | `node_id`, `hops=2` |
| 21 | `/similar_nodes` | GET | 用邻居集合 Jaccard + embedding 计算节点相似度 | `node_id` |

### 算子开发规范

新增一个算子的标准步骤（四步，缺一不可）：

```
1. src/api/semantic/xxx.py        ← 业务逻辑函数（接收 store/graph/ontology，返回 dict）
2. src/operators/xxx_op.py        ← SemanticOperator 包装（name 属性 + execute 方法）
3. src/operators/__init__.py      ← 加入 ALL_OPERATORS 列表
4. src/api/semantic/router.py     ← 注册 FastAPI 端点（@router.get/post）
```

必须遵守：
- 业务逻辑在 `api/semantic/xxx.py` 里，而不是在 `operators/xxx_op.py` 里——算子只做参数提取和路由
- 所有数据库访问通过 `store`/`graph` 参数，不直接 import 连接池
- 新算子必须同步写日志（`log.info("xxx: param=%s result_count=%d", ...)`）

---

## 9. 21 种 RST 篇章关系类型

RST（Rhetorical Structure Theory，修辞结构理论）关系用于描述相邻文本段落之间的语篇关系。Stage 2 用 LLM 对相邻段落对分类，结果存入 `t_rst_relation` 表，Stage 4 用来判断是否合并相邻段落重试抽取。

| 类别 | 关系类型 | 描述 |
|---|---|---|
| 因果/逻辑 | Cause-Result | A 导致 B（因在前，果在后） |
| | Result-Cause | B 是因为 A（果在前，因在后——叙述顺序与因果顺序相反） |
| | Purpose | A 是为了实现 B（目的关系） |
| | Means | B 是实现 A 的方法/路径 |
| 条件/使能 | Condition | 如果 A 则 B |
| | Unless | 除非 A，否则 B |
| | Enablement | A 使 B 成为可能（前提条件） |
| 精化/细化 | Elaboration | B 是对 A 的详述 |
| | Explanation | B 解释 A 的机制或原因 |
| | Restatement | B 换一种方式重新表达 A |
| | Summary | B 是 A 的摘要 |
| 对比/让步 | Contrast | A 和 B 形成对比 |
| | Concession | 虽然 A，但 B（承认 A，但 B 更重要） |
| 证据/评价 | Evidence | B 提供支持 A 观点的证据 |
| | Evaluation | B 对 A 进行评估或判断 |
| | Justification | B 为 A 的行为/决定提供理由 |
| 结构/组织 | Background | A 为理解 B 提供背景信息 |
| | Preparation | A 为 B 做铺垫（引导读者注意力） |
| | Sequence | A 在时间或逻辑上先于 B |
| | Joint | A 和 B 并列在同一层面（连接关系） |
| | Problem-Solution | A 提出问题，B 给出解决方案 |

**Stage 4 中的应用逻辑**：
只有 `Elaboration`、`Sequence`、`Restatement`、`Explanation` 这四种"连续型"关系才触发合并上下文重试——因为这四种关系表明相邻段落在讲述同一件事，合并后信息更完整。`Contrast`、`Concession` 等类型合并后反而会引入混淆，不合并。

---

## 10. 监控与质量评估

### 10.1 StatsCollector — 7 类指标

每 5 分钟采集一次，写入 `system_stats_snapshots.snapshot`（JSONB）：

```python
{
  "knowledge": {
    "documents": {"total", "by_status": {raw, cleaned, segmented, indexed}},
    "segments":  {"total", "by_type": {...}, "pending_alignment"},
    "facts":     {"total", "by_extraction_method": {llm, rule, cooccurrence}},
    "evidence":  {"total", "multi_evidence_facts"},
    "neo4j":     {"node_count", "rel_count", "rel_types": [...]}
  },
  "quality": {
    "avg_confidence": ...,
    "fact_coverage_rate": ...,      # 有 fact 的节点 / 总节点
    "conflict_open_count": ...,
    "weak_evidence_rate": ...       # confidence < 0.5 且 evidence < 2 的 fact 比例
  },
  "pipeline": {
    "queue_depth": ...,             # status='raw' 的文档数（积压量）
    "failed_docs": ...,
    "avg_segments_per_doc": ...
  },
  "evolution": {
    "candidates_by_status": {discovered, pending, pending_review, accepted, rejected},
    "recent_approvals_24h": ...
  },
  "sources": {
    "by_rank": {S: n, A: n, B: n, C: n},
    "by_extraction_method": {llm: n, rule: n, cooccurrence: n}
  }
}
```

### 10.2 OntologyQualityCalculator — 5 维度 20 指标

**G — 粒度（Granularity）**：
- G1: 度 Gini 系数（> 0.8 → 超级节点过多）
- G2: 超级节点数（degree > 20）
- G3: 孤立节点数（degree = 0）
- G4: 标签密度（segment_tags / 节点数）
- G5: 万金油节点（被太多不同类型段落引用，可能粒度太粗）

**O — 正交性（Orthogonality）**：
- O1: 谓语重叠度（同一对节点有多种谓语）
- O2: 谓语分布偏斜（个别谓语占比 > 30%）
- O3: 谓语利用率（实际使用的谓语 / 定义的谓语）
- O4: 节点语义相似度（embedding 过近的兄弟节点，可能应合并）

**L — 层间连通（Cross-layer）**：
- L1: 五层覆盖率（每层是否有跨层边）
- L2: 短路边（直接连跨两层以上，绕过中间层）
- L3: 完整五层链路数量（concept→mechanism→method→condition→scenario 的完整路径）

**D — 可发现性（Discoverability）**：
- D1: 别名覆盖率（有别名的节点 / 总节点）
- D2: 关系类型利用率（实际 used / defined）
- D3: 段落标签命中率（打了标签的段落 / 总段落）

**S — 结构健康（Structural）**：
- S1: 弱连通分量数（> 1 说明图断裂）
- S2: 依赖环（facts 中 A depends_on B + B depends_on A）
- S3: 平均最短路径长度
- S4: 概念节点平均深度（父子层级）

---

## 11. Worker 进程详细规格

**文件**：`worker.py`

4 个守护线程通过 `threading.Event` 协调优雅退出（Ctrl+C 会设置 `_stop_event`，所有线程检测到后自行结束）。

```python
_stop_event = threading.Event()

threads = [
    Thread(target=_crawler_thread, name="crawler", daemon=True),
    Thread(target=_pipeline_thread, name="pipeline", daemon=True),
    Thread(target=_stats_thread, name="stats", daemon=True),
    Thread(target=_maintenance_thread, name="maintenance", daemon=True),
]
```

**_pipeline_thread 关键逻辑**：

```python
def _pipeline_thread():
    while not _stop_event.is_set():
        # 1. 强制检查 LLM 可用性
        if not app.llm.is_enabled():
            log.warning("[pipeline] LLM not available — sleeping 120s")
            _stop_event.wait(120)
            continue

        # 2. 取 PIPELINE_LIMIT 篇 raw 文档
        raw_docs = app.store.fetchall(
            "SELECT * FROM documents WHERE status='raw' ORDER BY created_at LIMIT %s",
            (PIPELINE_LIMIT,)
        )
        if not raw_docs:
            _stop_event.wait(SLEEP_SECS)
            continue

        # 3. 每篇文档处理前二次检查 LLM（避免长批次中途 LLM 恢复后继续用弱路径）
        for doc in raw_docs:
            if not app.llm.is_enabled():
                log.warning("[pipeline] LLM went down mid-batch, stopping")
                break
            pipeline.process(PipelineContext(source_doc_id=doc["source_doc_id"]))
```

**_maintenance_thread 关键逻辑**：

```python
def _maintenance_thread():
    while not _stop_event.is_set():
        # 计算到下一个上海时间 03:00 的等待秒数
        sleep_secs = _seconds_until_next_3am_cst()
        log.info("[maintenance] sleeping %ds until 03:00 CST", sleep_secs)
        _stop_event.wait(sleep_secs)
        if _stop_event.is_set():
            break
        maintenance = OntologyMaintenance(app.store, app.graph, app.ontology)
        maintenance.run()
```

```python
def _seconds_until_next_3am_cst():
    import datetime
    tz = datetime.timezone(datetime.timedelta(hours=8))
    now = datetime.datetime.now(tz)
    target = now.replace(hour=3, minute=0, second=0, microsecond=0)
    if now.hour >= 3:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()
```

---

## 12. 开发约定与不变量

### 12.1 新增算子（四步，缺一不可）

```
1. src/api/semantic/xxx.py    ← 业务逻辑
2. src/operators/xxx_op.py    ← SemanticOperator 包装
3. src/operators/__init__.py  ← 注册到 ALL_OPERATORS
4. src/api/semantic/router.py ← FastAPI 端点
```

### 12.2 新增 Pipeline Stage

```
1. src/pipeline/stages/stageN_xxx.py ← 继承 Stage，实现 process(ctx, app)
2. src/pipeline/pipeline_factory.py  ← 加入管线链
```

### 12.3 本体变更（唯一正确流程）

```
1. 编辑 ontology/domains/*.yaml 或 ontology/top/relations.yaml
2. python scripts/load_ontology.py
3. 不要直接编辑 Neo4j 或 PostgreSQL 里的本体数据
```

### 12.4 必须遵守的不变量

1. **governance. 前缀**：所有 `evolution_candidates`、`conflict_records`、`review_records`、`ontology_versions` 的 SQL 必须带 `governance.` 前缀。开发模式 FakePostgres 会自动剥离，不影响 dev 运行。

2. **不混用两个 store**：Pipeline Stage 只用 `app.store`（telecom_kb），不访问 `app.crawler_store`。唯一例外是 Stage 3、4 在完成后向 `crawler_store` 写 `extraction_jobs` 记录（用于爬虫端的任务状态追踪）。

3. **短术语词边界**：凡是长度 ≤ 3 的别名，alias 匹配时必须用 `\b{alias}\b` 正则，不能直接 `in text`。已有代码在 `_find_terms()` 里强制执行，新代码不要绕过。

4. **SimHash + Jaccard 双重确认**：Stage 5 的段落去重必须 SimHash（Hamming ≤ 3）加 Jaccard（≥ 0.85）双重确认，不能只用 SimHash 单路。

5. **所有新代码必须同步加日志**，使用 `log = logging.getLogger(__name__)`，不事后补。

6. **禁止硬编码阈值**：评分权重、门控阈值、匹配阈值必须从 `evolution_policy.yaml` 或 `settings.py` 读取，不能写进代码里。