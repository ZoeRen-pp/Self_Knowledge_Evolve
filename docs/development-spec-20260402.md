# 语义知识操作系统 — 开发方案文档

**版本**：v0.3
**日期**：2026-04-02

---

# 一、数据库表结构

## 1.1 知识库 telecom_kb — public schema

### documents

```sql
CREATE TABLE documents (
    id                  BIGSERIAL PRIMARY KEY,
    source_doc_id       UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    crawl_task_id       BIGINT,                        -- 逻辑引用 crawler DB（跨库无物理 FK）
    site_key            VARCHAR(64)  NOT NULL,
    source_url          TEXT         NOT NULL,
    canonical_url       TEXT,
    title               TEXT,
    doc_type            VARCHAR(32),                    -- spec|vendor_doc|pdf|faq|tutorial|tech_article
    language            CHAR(5)      NOT NULL DEFAULT 'en',
    source_rank         CHAR(1)      NOT NULL,          -- S|A|B|C
    publish_time        TIMESTAMPTZ,
    crawl_time          TIMESTAMPTZ  NOT NULL,
    version_hint        VARCHAR(128),
    content_hash        CHAR(64),                       -- SHA-256（原始文档）
    normalized_hash     CHAR(64),                       -- SHA-256（清洗后文本）
    raw_storage_uri     TEXT,                           -- MinIO raw/ 路径
    cleaned_storage_uri TEXT,                           -- MinIO cleaned/ 路径
    struct_storage_uri  TEXT,
    page_structure      JSONB,
    status              VARCHAR(32)  NOT NULL DEFAULT 'raw',
                        -- raw → cleaned → segmented → indexed | deduped | low_quality
    dedup_group_id      UUID,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### segments

```sql
CREATE TABLE segments (
    id                  BIGSERIAL PRIMARY KEY,
    segment_id          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    source_doc_id       UUID         NOT NULL REFERENCES documents(source_doc_id),
    section_path        TEXT[],                         -- ['第3章','3.2','3.2.1']
    section_title       TEXT,
    segment_index       INTEGER      NOT NULL,
    segment_type        VARCHAR(32)  NOT NULL,
                        -- definition|mechanism|constraint|config|fault|troubleshooting
                        -- best_practice|performance|comparison|table|code|unknown
    raw_text            TEXT         NOT NULL,
    normalized_text     TEXT,
    token_count         INTEGER,
    confidence          NUMERIC(4,3) DEFAULT 1.0,
    dedup_signature     CHAR(64),
    simhash_value       BIGINT,                        -- 64-bit SimHash
    embedding_ref       TEXT,
    embedding           vector(1024),                   -- BAAI/bge-m3 段落嵌入
    title               VARCHAR(255),                   -- EDU 标题（LLM 生成或 section_title）
    title_vec           vector(1024),                   -- title 向量嵌入
    content_vec         vector(1024),                   -- content 向量嵌入
    content_source      VARCHAR(128),                   -- '{site_key}:{canonical_url}'
    lifecycle_state     VARCHAR(32)  NOT NULL DEFAULT 'active',
                        -- active|superseded|pending_alignment|deprecated
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

**各字段写入时机**：

| 字段 | 写入阶段 | 说明 |
|------|----------|------|
| segment_id ~ simhash_value | Stage 2 | 切段时写入 |
| title, content_source | Stage 2 | 切段时同步写入 |
| embedding | Stage 6 | 段落嵌入向量回填 |
| title_vec, content_vec | Stage 6 | 标题/内容嵌入向量回填 |
| lifecycle_state 变更 | Stage 3/5 | pending_alignment(Stage 3)、superseded(Stage 5) |

### t_rst_relation

```sql
CREATE TABLE t_rst_relation (
    nn_relation_id  VARCHAR(36)   NOT NULL PRIMARY KEY,
    relation_type   VARCHAR(255)  NOT NULL,             -- 21 种 RST 类型
    src_edu_id      UUID          NOT NULL REFERENCES segments(segment_id),
    dst_edu_id      UUID          NOT NULL REFERENCES segments(segment_id),
    meta_context    JSONB,                              -- {"SYNTACTIC_ORDER": <int>, "src_type": ..., "dst_type": ...}
    relation_source VARCHAR(255),                       -- rule | llm
    update_time     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    reliability     BIGINT        NOT NULL DEFAULT 1
);
```

**relation_type 取值（21 种，6 类）**：

| 类别 | 类型 | 语义 |
|------|------|------|
| 因果逻辑 | Cause-Result | A 导致 B |
| | Result-Cause | B 是因为 A |
| | Purpose | A 是为了 B |
| | Means | 通过 B 实现 A |
| 条件/使能 | Condition | 如果 A 则 B |
| | Unless | 除非 A 否则 B |
| | Enablement | A 使 B 成为可能 |
| 展开/细化 | Elaboration | B 对 A 细化展开 |
| | Explanation | B 解释 A 的原理 |
| | Restatement | B 换种方式复述 A |
| | Summary | B 总结 A |
| 对比/让步 | Contrast | A 和 B 形成对比 |
| | Concession | 尽管 A 但 B |
| 证据/评价 | Evidence | B 为 A 提供证据 |
| | Evaluation | B 对 A 做出评价 |
| | Justification | B 为 A 的决策提供理由 |
| 结构/组织 | Background | A 为理解 B 提供背景 |
| | Preparation | A 为 B 做铺垫 |
| | Sequence | A 在 B 之前 |
| | Joint | A 和 B 并列同层级 |
| | Problem-Solution | A 提出问题 B 给出方案 |

判定方式：LLM 启用时从 21 种中选择；LLM 关闭时 37 条规则映射回退，未命中默认 Sequence。`relation_source` 字段记录来源（`llm` / `rule`）。

### segment_tags

```sql
CREATE TABLE segment_tags (
    id               BIGSERIAL PRIMARY KEY,
    segment_id       UUID         NOT NULL REFERENCES segments(segment_id),
    tag_type         VARCHAR(32)  NOT NULL,             -- canonical|semantic_role|context|mechanism_tag|method_tag|condition_tag|scenario_tag
    tag_value        VARCHAR(256) NOT NULL,
    ontology_node_id VARCHAR(128),                      -- 对应本体 node_id
    confidence       NUMERIC(4,3) DEFAULT 1.0,
    tagger           VARCHAR(64),                       -- rule|llm|manual
    ontology_version VARCHAR(32),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### facts

```sql
CREATE TABLE facts (
    id               BIGSERIAL PRIMARY KEY,
    fact_id          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    subject          VARCHAR(256) NOT NULL,              -- 本体 node_id
    predicate        VARCHAR(128) NOT NULL,              -- 受控关系类型
    object           VARCHAR(256) NOT NULL,              -- 本体 node_id
    qualifier        JSONB,
    domain           VARCHAR(128),
    confidence       NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    lifecycle_state  VARCHAR(32)  NOT NULL DEFAULT 'active',
                     -- active|superseded|conflicted
    merge_cluster_id UUID,
    ontology_version VARCHAR(32),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### evidence

```sql
CREATE TABLE evidence (
    id                  BIGSERIAL PRIMARY KEY,
    evidence_id         UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id             UUID         NOT NULL REFERENCES facts(fact_id),
    source_doc_id       UUID         NOT NULL REFERENCES documents(source_doc_id),
    segment_id          UUID         REFERENCES segments(segment_id),
    exact_span          TEXT,
    span_offset_start   INTEGER,
    span_offset_end     INTEGER,
    source_rank         CHAR(1)      NOT NULL,
    extraction_method   VARCHAR(64),                     -- rule|llm
    evidence_score      NUMERIC(4,3) DEFAULT 0.5,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### lexicon_aliases

```sql
CREATE TABLE lexicon_aliases (
    id               BIGSERIAL PRIMARY KEY,
    alias_id         UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_form     TEXT         NOT NULL,
    canonical_node_id VARCHAR(128) NOT NULL,
    alias_type       VARCHAR(32)  NOT NULL,              -- abbreviation|full_name|vendor_term|alternate_spelling
    vendor           VARCHAR(64),
    language         CHAR(5)      DEFAULT 'en',
    confidence       NUMERIC(4,3) DEFAULT 1.0,
    source_doc_id    UUID         REFERENCES documents(source_doc_id),
    ontology_version VARCHAR(32),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (surface_form, canonical_node_id)
);
```

## 1.2 知识库 telecom_kb — governance schema

### governance.evolution_candidates

```sql
CREATE TABLE governance.evolution_candidates (
    id                       BIGSERIAL PRIMARY KEY,
    candidate_id             UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_forms            TEXT[]       NOT NULL,
    normalized_form          VARCHAR(256),
    candidate_parent_id      VARCHAR(128),
    source_count             INTEGER      NOT NULL DEFAULT 0,
    source_diversity_score   NUMERIC(4,3) DEFAULT 0.0,
    temporal_stability_score NUMERIC(4,3) DEFAULT 0.0,
    structural_fit_score     NUMERIC(4,3) DEFAULT 0.0,
    retrieval_gain_score     NUMERIC(4,3) DEFAULT 0.0,
    synonym_risk_score       NUMERIC(4,3) DEFAULT 0.0,
    composite_score          NUMERIC(4,3) DEFAULT 0.0,
    review_status            VARCHAR(32)  NOT NULL DEFAULT 'discovered',
                             -- discovered|scored|pending_review|auto_accepted|rejected
    reviewer                 VARCHAR(128),
    review_note              TEXT,
    first_seen_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    accepted_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### governance.conflict_records

```sql
CREATE TABLE governance.conflict_records (
    id             BIGSERIAL PRIMARY KEY,
    conflict_id    UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id_a      UUID         NOT NULL REFERENCES public.facts(fact_id),
    fact_id_b      UUID         NOT NULL REFERENCES public.facts(fact_id),
    conflict_type  VARCHAR(64)  NOT NULL,               -- contradictory_value
    description    TEXT,
    resolution     VARCHAR(32)  DEFAULT 'open',
    resolved_by    VARCHAR(128),
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### governance.ontology_versions

```sql
CREATE TABLE governance.ontology_versions (
    id             SERIAL PRIMARY KEY,
    version_tag    VARCHAR(32)  NOT NULL UNIQUE,
    description    TEXT,
    snapshot_uri   TEXT,
    diff_from_prev JSONB,
    published_by   VARCHAR(128),
    published_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status         VARCHAR(32)  NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### governance.review_records

```sql
CREATE TABLE governance.review_records (
    id           BIGSERIAL PRIMARY KEY,
    review_id    UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    object_type  VARCHAR(64)  NOT NULL,
    object_id    UUID         NOT NULL,
    action       VARCHAR(64)  NOT NULL,
    reviewer     VARCHAR(128) NOT NULL,
    note         TEXT,
    before_state JSONB,
    after_state  JSONB,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.3 爬虫库 telecom_crawler

### source_registry

```sql
CREATE TABLE source_registry (
    id              SERIAL PRIMARY KEY,
    site_key        VARCHAR(64)   NOT NULL UNIQUE,
    site_name       VARCHAR(255)  NOT NULL,
    home_url        VARCHAR(1024) NOT NULL,
    source_rank     CHAR(1)       NOT NULL CHECK (source_rank IN ('S','A','B','C')),
    crawl_enabled   BOOLEAN       NOT NULL DEFAULT true,
    robots_policy   JSONB,
    rate_limit_rps  NUMERIC(5,2)  DEFAULT 1.0,
    seed_urls       JSONB,
    scope_rules     JSONB,
    extra_headers   JSONB,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);
```

### crawl_tasks

```sql
CREATE TABLE crawl_tasks (
    id              BIGSERIAL PRIMARY KEY,
    site_key        VARCHAR(64)  NOT NULL REFERENCES source_registry(site_key),
    url             TEXT         NOT NULL,
    canonical_url   TEXT,
    task_type       VARCHAR(32)  NOT NULL DEFAULT 'full',
    priority        SMALLINT     NOT NULL DEFAULT 5,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
                    -- pending|running|done|failed|skipped|deduped
    scheduled_at    TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    retry_count     SMALLINT     NOT NULL DEFAULT 0,
    http_status     SMALLINT,
    error_msg       TEXT,
    parent_task_id  BIGINT       REFERENCES crawl_tasks(id),
    raw_storage_uri TEXT,
    content_hash    CHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

### extraction_jobs

```sql
CREATE TABLE extraction_jobs (
    id               BIGSERIAL PRIMARY KEY,
    job_id           UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    job_type         VARCHAR(64)  NOT NULL,
    source_doc_id    UUID,                              -- 逻辑引用 documents.source_doc_id
    status           VARCHAR(32)  NOT NULL DEFAULT 'pending',
    pipeline_version VARCHAR(32),
    config_snapshot  JSONB,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    error_msg        TEXT,
    stats            JSONB,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.4 Neo4j Schema

### 节点标签 + 唯一约束

| 标签 | 约束字段 |
|------|----------|
| OntologyNode | node_id |
| MechanismNode | node_id |
| MethodNode | node_id |
| ConditionRuleNode | node_id |
| ScenarioPatternNode | node_id |
| Alias | alias_id |
| CandidateConcept | candidate_id |
| KnowledgeSegment | segment_id |
| SourceDocument | source_doc_id |
| Fact | fact_id |
| Evidence | evidence_id |
| OntologyVersion | version_tag |

### 关系边类型

| 边类型 | 方向 | 说明 |
|--------|------|------|
| SUBCLASS_OF | child → parent | 本体层次 |
| ALIAS_OF | Alias → OntologyNode | 别名映射 |
| RELATED_TO | node → node | 通用关系（带 predicate 属性） |
| BELONGS_TO | Segment → Document | 段落归属 |
| TAGGED_WITH | Segment → OntologyNode | 本体标签 |
| SUPPORTED_BY | Fact → Evidence | 事实溯源 |
| EXTRACTED_FROM | Evidence → Segment | 证据来源 |

---

# 二、本体 YAML 结构

## 2.1 文件布局

```
ontology/
├── top/
│   └── relations.yaml          # 54 种受控关系类型
├── domains/
│   ├── ip_network.yaml         # 74 concept 节点
│   ├── ip_network_mechanisms.yaml  # 24 mechanism 节点
│   ├── ip_network_methods.yaml     # 22 method 节点
│   ├── ip_network_conditions.yaml  # 20 condition 节点
│   └── ip_network_scenarios.yaml   # 13 scenario 节点
├── lexicon/
│   └── aliases.yaml            # 156 条别名
└── governance/
    └── evolution_policy.yaml   # 演化门控阈值
```

## 2.2 节点格式

```yaml
- id: IP.BGP
  canonical_name: BGP
  display_name_zh: 边界网关协议
  parent_id: IP.ROUTING
  knowledge_layer: concept        # concept|mechanism|method|condition|scenario
  maturity_level: core            # core|extended|experimental
  lifecycle_state: active         # active|deprecated
  description: "Border Gateway Protocol, the de-facto inter-AS routing protocol..."
  aliases: [BGP-4, eBGP, iBGP]
  allowed_relations: [uses_protocol, depends_on, impacts]
  source_basis: [RFC 4271]
```

## 2.3 别名格式

```yaml
- surface_form: "border gateway protocol"
  canonical_node_id: IP.BGP
  alias_type: full_name           # full_name|abbreviation|vendor_term|alternate_spelling
  language: en
  confidence: 1.0
```

## 2.4 演化策略

```yaml
candidate_admission:
  min_source_count: 3
  min_source_diversity: 0.6
  min_temporal_stability: 0.7
  min_structural_fit: 0.65
  min_composite_score: 0.65
  synonym_risk_max: 0.4
  auto_accept_threshold: 0.85

anti_drift:
  min_days_in_candidate_pool: 7

scoring_weights:
  source_authority: 0.25
  source_diversity: 0.20
  temporal_stability: 0.20
  structural_fit: 0.20
  retrieval_gain: 0.10
  synonym_risk_penalty: 0.05
```

---

# 三、7 阶段流水线详细规则

## Stage 1：清洗（Ingest/Clean）

**输入**：`source_doc_id`（documents 表中 status='raw' 的记录）

**处理规则**：

| 规则 | 说明 |
|------|------|
| C3 | content_hash 去重：SHA-256(原始文档) 与已有文档对比，重复 → status='deduped' |
| C4 | 正文提取：trafilatura → readability → 正则去标签（三级降级）；质量门控：token < 200 → status='low_quality' |
| C5 | 文档类型检测：URL/标题正则 → spec/vendor_doc/pdf/faq/tutorial/tech_article |

**处理流程**：
1. 从 documents 表加载文档记录
2. 从 MinIO 加载 raw 内容（raw_storage_uri）
3. content_hash 去重检查
4. ContentExtractor.extract() — 正文提取 + 标题 + 语言检测
5. DocumentNormalizer.normalize() — 去噪归一化（5 类样板正则 + 重复段落 + Unicode NFKC）
6. 质量门控（token < 200 → low_quality）
7. doc_type 检测
8. 清洗后文本存入 MinIO cleaned/
9. 更新 documents：content_hash, normalized_hash, cleaned_storage_uri, doc_type, status='cleaned'

**输出**：documents.status → 'cleaned'

## Stage 2：切段 + RST（Segment）

**输入**：source_doc_id（status='cleaned'）

**处理规则**：

| 规则 | 说明 |
|------|------|
| S1 | 结构切分：Markdown 标题 / RFC 编号节标题 / 纯文本段落 → 自动检测 |
| S2 | 语义角色分类：12 类正则模式匹配（definition/mechanism/config/constraint/fault/troubleshooting/best_practice/performance/comparison/table/code/unknown） |
| S3 | 长度控制：< 30 token 丢弃；> 1024 token 滑窗切分（window=512, overlap=64） |
| S4 | RST 关系：相邻 EDU 对生成修辞关系（21 种类型，LLM 或规则回退） |

**DB 写入**：
- `segments`：segment_id, raw_text, segment_type, title, content_source, simhash_value, confidence
- `t_rst_relation`：relation_type, src_edu_id, dst_edu_id, relation_source
- `documents.status → 'segmented'`

## Stage 3：本体对齐（Align）

**输入**：source_doc_id

**处理规则**：

| 规则 | 说明 |
|------|------|
| A1 | 精确/别名匹配：遍历 alias_map，短术语(≤3字符)严格词边界匹配 |
| A2 | 置信度：精确匹配 1.0，别名匹配 0.9 |
| A3 | 候选发现：未匹配的 CamelCase 术语 → governance.evolution_candidates（UPSERT，累加 source_count） |
| A4 | 语义角色标签：segment_type → 中文标签（定义/机制/配置/约束/…） |
| A5 | 上下文标签：正则检测 data center/campus/5GC 等场景 |

**DB 写入**：
- `segment_tags`：canonical / semantic_role / context / mechanism_tag / method_tag / condition_tag / scenario_tag
- `governance.evolution_candidates`：新候选或 source_count+1

## Stage 3b：本体演化（Evolve）

**输入**：source_doc_id（处理该文档中发现的候选）

**评分维度**：

| 维度 | 计算方式 |
|------|----------|
| source_diversity | 贡献文档的不同 site_key 数 / 3.0 |
| temporal_stability | 候选池存活天数 / 14.0 |
| structural_fit | 与已有本体节点的 Jaccard 词重叠 |
| synonym_risk | 与已有别名的子串重叠率 |
| composite_score | 加权综合（source_authority 0.25 + diversity 0.20 + stability 0.20 + fit 0.20 + gain 0.10 - synonym_risk 0.05） |

**门控**：6 项全过 → 若 composite ≥ 0.85 且 ≥ 7 天 → 自动创建 Neo4j 节点（EVOLVED.* node_id）；否则 → pending_review。

## Stage 4：关系抽取（Extract）

**输入**：source_doc_id

**处理规则**：

| 规则 | 说明 |
|------|------|
| R1 | 15 条正则模式：uses_protocol, is_a, part_of, depends_on, requires, encapsulates, establishes, advertises, impacts, causes, implements, forwards_via, protects, monitored_by, configured_by |
| R2 | LLM 抽取（可选）：传入段落文本 + 候选 node_id + 有效关系 → 结构化三元组 |
| R3 | 主语/宾语必须解析到本体节点；谓语必须在 54 种受控关系中 |
| R4 | 置信度评分：5 维公式 |

**DB 写入**：`facts` + `evidence`

## Stage 5：去重（Dedup）

**处理规则**：

| 规则 | 说明 |
|------|------|
| D1-D2 | 段落去重：SimHash 汉明距离 ≤ 3 + Jaccard ≥ 0.85 → lifecycle_state='superseded' |
| D3 | 事实精确去重：(S, P, O) 完全匹配 → 同一 merge_cluster_id |
| D4-D5 | 冲突检测：同 S+P 不同 O → lifecycle_state='conflicted' + conflict_records 记录 |

## Stage 6：索引（Index）

**处理规则**：

| 规则 | 说明 |
|------|------|
| I1 | 置信度门控：segment ≥ 0.5, fact ≥ 0.5 |
| I2 | Neo4j 写入：SourceDocument / KnowledgeSegment / Fact / Evidence 节点 + 关系边 |
| I3 | 向量嵌入：segments.embedding + title_vec + content_vec（需 EMBEDDING_ENABLED=true） |

**DB 写入**：Neo4j 全量 + segments(embedding/title_vec/content_vec) + documents.status='indexed'

---

# 四、语义算子 API 规格

Base URL：`/api/v1/semantic`

## 4.1 查询与解析

### GET /lookup

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| term | string | Y | 查询术语 |
| scope | string | N | 限定域 |
| lang | string | N | 语言(默认 en) |
| include_evidence | bool | N | 是否返回证据 |
| max_evidence | int | N | 最大证据数(默认 3) |

### GET /resolve

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| alias | string | Y | 别名/厂商术语 |
| scope | string | N | 限定域 |
| vendor | string | N | 厂商过滤 |

## 4.2 图遍历

### GET /expand

| 参数 | 类型 | 说明 |
|------|------|------|
| node_id | string | 中心节点 |
| relation_types | list[string] | 关系类型过滤 |
| depth | int (1-3) | 展开深度 |
| min_confidence | float | 最低置信度 |
| include_facts | bool | 包含事实 |
| include_segments | bool | 包含段落 |

### GET /path

| 参数 | 说明 |
|------|------|
| start_node_id | 起点 |
| end_node_id | 终点 |
| relation_policy | all / causal / structural |
| max_hops | 最大跳数(1-8) |

### GET /dependency_closure

| 参数 | 说明 |
|------|------|
| node_id | 起点 |
| relation_types | 关系过滤 |
| max_depth | 最大深度(1-10) |
| include_optional | 包含可选依赖 |

## 4.3 影响分析

### POST /impact_propagate

```json
{
  "event_node_id": "IP.BGP",
  "event_type": "fault",
  "relation_policy": "causal",
  "max_depth": 4,
  "min_confidence": 0.6,
  "context": {}
}
```

### POST /filter

```json
{
  "object_type": "facts",
  "filters": {"subject": "IP.BGP"},
  "sort_by": "confidence",
  "sort_order": "desc",
  "page": 1,
  "page_size": 20
}
```

## 4.4 证据与治理

### GET /evidence_rank

| 参数 | 说明 |
|------|------|
| fact_id | 事实 UUID |
| rank_by | evidence_score / source_rank / created_at |
| max_results | 最大返回数 |

### GET /conflict_detect

| 参数 | 说明 |
|------|------|
| topic_node_id | 主题节点 |
| predicate | 谓语过滤(可选) |
| min_confidence | 最低置信度 |

### POST /fact_merge

```json
{
  "fact_ids": ["uuid1", "uuid2"],
  "merge_strategy": "highest_confidence",
  "canonical_fact": null
}
```

## 4.5 本体演化

### GET /candidate_discover

| 参数 | 说明 |
|------|------|
| window_days | 时间窗口 |
| min_frequency | 最低出现频次 |
| domain | 域过滤 |
| min_source_count | 最低来源数 |

### GET /attach_score

| 参数 | 说明 |
|------|------|
| candidate_id | 候选 UUID |
| candidate_parent_ids | 候选父节点列表 |

### POST /evolution_gate

```json
{"candidate_id": "uuid"}
```

## 4.6 语义搜索

### POST /semantic_search

```json
{
  "query": "BGP route reflector configuration",
  "top_k": 5,
  "min_similarity": 0.5,
  "layer_filter": "mechanism"
}
```

### POST /edu_search

```json
{
  "query": "OSPF area design",
  "top_k": 5,
  "min_similarity": 0.5,
  "title_weight": 0.3
}
```

---

# 五、数据源接入规范

## 5.1 接入方式

任何数据源只需两步即可接入 Pipeline：

1. **往 documents 表插入一条记录**：

```sql
INSERT INTO documents (
    source_doc_id, site_key, source_url, source_rank,
    crawl_time, raw_storage_uri, status
) VALUES (
    gen_random_uuid(), 'manual-import', '/path/to/file.html', 'B',
    NOW(), 'minio://telecom-kb-raw/raw/xxxx.html', 'raw'
);
```

2. **往 MinIO raw/ 存放原始文档**。

Pipeline worker 自动捞取 `status='raw'` 的文档处理。

## 5.2 爬虫数据源

Spider 爬完后自动完成上述两步：
- `_process_task()`：抓取 HTML → 存 MinIO raw/{sha256}.html → 更新 crawl_tasks
- `_create_document()`：创建 documents 记录（status='raw'）

## 5.3 文档状态生命周期

```
raw → cleaned → segmented → indexed
 │       │
 │       └→ low_quality
 └→ deduped
```

---

# 六、配置参数一览

## 必填项

| 参数 | 说明 | 示例 |
|------|------|------|
| POSTGRES_HOST | 知识库 PG 主机 | 127.0.0.1 |
| POSTGRES_PORT | 知识库 PG 端口 | 5432 |
| POSTGRES_DB | 知识库名 | telecom_kb |
| POSTGRES_USER | 用户名 | postgres |
| POSTGRES_PASSWORD | 密码 | |
| POSTGRES_POOL_MIN / MAX | 连接池 | 2 / 10 |
| NEO4J_URI | Neo4j 连接 | bolt://127.0.0.1:7687 |
| NEO4J_USER / PASSWORD | Neo4j 认证 | neo4j / pwd |
| MINIO_ENDPOINT | MinIO 地址 | 127.0.0.1:9000 |

## 可选项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| CRAWLER_POSTGRES_HOST | 同 POSTGRES_HOST | 爬虫库主机（留空复用主库） |
| CRAWLER_POSTGRES_PORT | 同 POSTGRES_PORT | 爬虫库端口（0=复用） |
| CRAWLER_POSTGRES_DB | telecom_crawler | 爬虫库名 |
| CRAWLER_POSTGRES_USER | 同 POSTGRES_USER | 爬虫库用户 |
| CRAWLER_POSTGRES_PASSWORD | 同 POSTGRES_PASSWORD | 爬虫库密码 |
| LLM_ENABLED | false | 启用 LLM 抽取 |
| LLM_API_KEY | | API 密钥 |
| LLM_BASE_URL | https://api.anthropic.com | API 地址 |
| LLM_MODEL | claude-haiku-4-5-20251001 | 模型 |
| EMBEDDING_ENABLED | false | 启用向量嵌入 |
| EMBEDDING_MODEL | BAAI/bge-m3 | 嵌入模型 |
| EMBEDDING_DEVICE | cpu | cpu 或 cuda |
| ONTOLOGY_VERSION | v0.2.0 | 本体版本 |
| LOG_LEVEL | INFO | 日志级别 |
| STARTUP_HEALTH_REQUIRED | true | 启动健康检查 |

---

# 七、本地开发模式

```bash
python run_dev.py
```

### 工作原理

`run_dev.py` 在任何 `src.db` 导入之前，将 3 个 fake 模块注入 `sys.modules`：

| 真实模块 | 替代模块 | 后端 |
|----------|----------|------|
| src.db.postgres | src.dev.fake_postgres | SQLite :memory: |
| src.db.crawler_postgres | src.dev.fake_crawler_postgres | SQLite :memory: |
| src.db.neo4j_client | src.dev.fake_neo4j | dict |

然后调用 `seed_from_registry()` 从 YAML 本体 seed 数据到假库。

fake_postgres 的 `_to_sqlite()` 自动处理：
- `%s` → `?`（占位符转换）
- `%s::jsonb` → `?`（PG 类型转换剥离）
- `governance.` → ``（schema 前缀剥离）

---

# 八、项目文件结构

```
Self_Knowledge_Evolve/
├── semcore/semcore/                    # 框架包（零外部依赖）
│   ├── core/types.py                   # 领域数据类
│   ├── core/context.py                 # PipelineContext
│   ├── providers/base.py               # 5 个 Provider ABC
│   ├── ontology/base.py                # OntologyProvider ABC
│   ├── governance/base.py              # 治理三件套 ABC
│   ├── operators/base.py               # 算子 + 中间件 + 注册表
│   ├── pipeline/base.py                # Stage + Pipeline
│   └── app.py                          # AppConfig + SemanticApp
│
├── src/
│   ├── app.py                          # FastAPI 入口
│   ├── app_factory.py                  # build_app() 组合根
│   ├── config/settings.py              # Pydantic Settings
│   ├── db/
│   │   ├── postgres.py                 # 知识库连接池
│   │   ├── crawler_postgres.py         # 爬虫库连接池
│   │   └── neo4j_client.py             # Neo4j driver
│   ├── providers/                      # 6 个 Provider 实现
│   │   ├── postgres_store.py
│   │   ├── crawler_postgres_store.py
│   │   ├── neo4j_store.py
│   │   ├── anthropic_llm.py
│   │   ├── bge_m3_embedding.py
│   │   └── minio_store.py
│   ├── ontology/
│   │   ├── registry.py                 # OntologyRegistry（单例，YAML → 内存）
│   │   ├── yaml_provider.py            # YAMLOntologyProvider
│   │   └── validator.py                # YAML 校验
│   ├── governance/
│   │   ├── confidence_scorer.py        # 5 维置信度
│   │   ├── conflict_detector.py        # 冲突检测
│   │   └── evolution_gate.py           # 6 项门控
│   ├── pipeline/
│   │   ├── pipeline_factory.py         # build_pipeline() → 7 阶段
│   │   ├── preprocessing/
│   │   │   ├── extractor.py            # HTML 正文提取
│   │   │   └── normalizer.py           # 去噪归一化
│   │   └── stages/
│   │       ├── stage1_ingest.py        # 清洗（C3-C5）
│   │       ├── stage2_segment.py       # 切段 + RST（S1-S4）
│   │       ├── stage3_align.py         # 本体对齐（A1-A5）
│   │       ├── stage3b_evolve.py       # 本体演化
│   │       ├── stage4_extract.py       # 关系抽取（R1-R4）
│   │       ├── stage5_dedup.py         # 去重（D1-D5）
│   │       └── stage6_index.py         # 索引（I1-I3）
│   ├── operators/                      # 15 个 SemanticOperator
│   ├── api/semantic/                   # 算子业务逻辑 + router.py
│   ├── crawler/
│   │   └── spider.py                   # HTTP 爬虫（Pipeline 外部数据源）
│   ├── utils/                          # 文本/哈希/置信度/嵌入/LLM/日志
│   └── dev/                            # fake_postgres + fake_crawler_postgres + fake_neo4j + seed
│
├── ontology/                           # 本体 YAML（唯一源头）
│   ├── domains/                        # 5 个领域文件，153 节点
│   ├── lexicon/aliases.yaml            # 156 条别名
│   ├── top/relations.yaml              # 54 种关系类型
│   └── governance/evolution_policy.yaml
│
├── scripts/
│   ├── init_postgres.sql               # 知识库 DDL
│   ├── init_crawler_postgres.sql       # 爬虫库 DDL
│   ├── init_neo4j.py                   # 约束 + 索引
│   ├── load_ontology.py                # YAML → Neo4j + PG
│   └── migrations/
│
├── worker.py                           # 后台 Worker（爬取 + Pipeline 调度）
├── run_dev.py                          # 本地开发（内存模式）
└── .env.example                        # 配置模板
```