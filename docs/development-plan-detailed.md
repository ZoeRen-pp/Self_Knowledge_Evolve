# 网络通信领域语义知识库 — 开发方案细化文档

**版本**：v0.1
**状态**：开发方案初稿
**基于**：系统设计文档 v0.1 + 本体设计文档 v0.1

---

# 一、数据库表结构（PostgreSQL）

> **v0.3 变更**：以下 3 张表（source_registry、crawl_tasks、extraction_jobs）已迁入独立数据库 `telecom_crawler`。DDL 见 `scripts/init_crawler_postgres.sql`，配置项 `CRAWLER_POSTGRES_*`。`documents.crawl_task_id` 和 `documents.site_key` 的外键已降级为逻辑引用（跨库不支持物理 FK）。

## 1.1 来源注册表 source_registry

```sql
CREATE TABLE source_registry (
    id              SERIAL PRIMARY KEY,
    site_key        VARCHAR(64)  NOT NULL UNIQUE,  -- 站点唯一标识，如 "ietf", "cisco"
    site_name       VARCHAR(255) NOT NULL,
    home_url        VARCHAR(1024) NOT NULL,
    source_rank     CHAR(1)      NOT NULL CHECK (source_rank IN ('S','A','B','C')),
    crawl_enabled   BOOLEAN      NOT NULL DEFAULT true,
    robots_policy   JSONB,                         -- robots.txt 解析结果缓存
    rate_limit_rps  NUMERIC(5,2) DEFAULT 1.0,      -- 每秒请求限制
    seed_urls       JSONB,                         -- 初始种子URL列表
    scope_rules     JSONB,                         -- 允许/拒绝路径正则规则
    extra_headers   JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.2 抓取任务表 crawl_tasks

```sql
CREATE TABLE crawl_tasks (
    id              BIGSERIAL    PRIMARY KEY,
    site_key        VARCHAR(64)  NOT NULL REFERENCES source_registry(site_key),
    url             TEXT         NOT NULL,
    canonical_url   TEXT,
    task_type       VARCHAR(32)  NOT NULL DEFAULT 'full',  -- full | incremental | retry
    priority        SMALLINT     NOT NULL DEFAULT 5,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
                    -- pending | running | done | failed | skipped | deduped
    scheduled_at    TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    retry_count     SMALLINT     NOT NULL DEFAULT 0,
    http_status     SMALLINT,
    error_msg       TEXT,
    parent_task_id  BIGINT       REFERENCES crawl_tasks(id),
    raw_storage_uri TEXT,        -- 原始HTML在对象存储中的路径
    content_hash    CHAR(64),    -- SHA-256 of raw content
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_crawl_tasks_status    ON crawl_tasks(status);
CREATE INDEX idx_crawl_tasks_site_key  ON crawl_tasks(site_key);
CREATE UNIQUE INDEX idx_crawl_tasks_url ON crawl_tasks(url);
```

## 1.3 文档元数据表 documents

```sql
CREATE TABLE documents (
    id                  BIGSERIAL    PRIMARY KEY,
    source_doc_id       UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    crawl_task_id       BIGINT       REFERENCES crawl_tasks(id),
    site_key            VARCHAR(64)  NOT NULL REFERENCES source_registry(site_key),
    source_url          TEXT         NOT NULL,
    canonical_url       TEXT,
    title               TEXT,
    doc_type            VARCHAR(32),
                        -- spec | vendor_doc | tech_article | faq | tutorial | pdf | unknown
    language            CHAR(5)      NOT NULL DEFAULT 'en',
    source_rank         CHAR(1)      NOT NULL,
    publish_time        TIMESTAMPTZ,
    crawl_time          TIMESTAMPTZ  NOT NULL,
    version_hint        VARCHAR(128),              -- 文档版本号提示
    content_hash        CHAR(64),                  -- 原始内容 SHA-256
    normalized_hash     CHAR(64),                  -- 去模板正文 SHA-256
    raw_storage_uri     TEXT,
    cleaned_storage_uri TEXT,
    struct_storage_uri  TEXT,                      -- 结构化解析结果路径
    page_structure      JSONB,                     -- 文档章节树摘要
    status              VARCHAR(32)  NOT NULL DEFAULT 'raw',
                        -- raw | cleaned | segmented | indexed | superseded | deprecated
    dedup_group_id      UUID,                      -- 与内容相同文档归为同一组
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_documents_canonical_url  ON documents(canonical_url);
CREATE INDEX idx_documents_normalized_hash ON documents(normalized_hash);
CREATE INDEX idx_documents_site_key       ON documents(site_key);
CREATE INDEX idx_documents_status         ON documents(status);
```

## 1.4 知识片段表 segments

> **v0.3 变更**：原 `t_edu_detail` 表已合并入 `segments`，新增 `title`、`title_vec`、`content_vec`、`content_source` 四列，`embedding` 列保留为段落整体嵌入向量。迁移脚本：`scripts/migrations/002_merge_edu_into_segments.sql`。

```sql
CREATE TABLE segments (
    id                  BIGSERIAL    PRIMARY KEY,
    segment_id          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    source_doc_id       UUID         NOT NULL REFERENCES documents(source_doc_id),
    section_path        TEXT[],                    -- ['第3章','3.2','3.2.1']
    section_title       TEXT,
    segment_index       INTEGER      NOT NULL,     -- 同文档内顺序
    segment_type        VARCHAR(32)  NOT NULL,
                        -- definition|mechanism|constraint|config|example
                        -- fault|troubleshooting|best_practice|performance|comparison|table|code
    raw_text            TEXT         NOT NULL,
    normalized_text     TEXT,
    token_count         INTEGER,
    confidence          NUMERIC(4,3) DEFAULT 1.0,  -- 0~1
    dedup_signature     CHAR(64),                  -- SimHash 或 MinHash 签名
    simhash_value       BIGINT,                    -- 64-bit SimHash，用于汉明距离查询
    embedding_ref       TEXT,                      -- 向量索引中的 ID 或 URI
    embedding           vector(1024),              -- BAAI/bge-m3 段落嵌入向量
    title               VARCHAR(255),              -- EDU 标题（LLM 生成或 section_title 回退）
    title_vec           vector(1024),              -- title 的 bge-m3 向量嵌入
    content_vec         vector(1024),              -- raw_text 的 bge-m3 向量嵌入
    content_source      VARCHAR(128),              -- 来源标识 '{site_key}:{canonical_url}'
    lifecycle_state     VARCHAR(32)  NOT NULL DEFAULT 'active',
                        -- active | superseded | deprecated | conflicted | pending_alignment
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_segments_source_doc_id   ON segments(source_doc_id);
CREATE INDEX idx_segments_segment_type    ON segments(segment_type);
CREATE INDEX idx_segments_simhash_value   ON segments(simhash_value);
-- 向量 ANN 索引（数据批量加载后创建）：
-- CREATE INDEX ON segments USING hnsw (embedding    vector_cosine_ops) WITH (m=16, ef_construction=64);
-- CREATE INDEX ON segments USING hnsw (title_vec    vector_cosine_ops) WITH (m=16, ef_construction=64);
-- CREATE INDEX ON segments USING hnsw (content_vec  vector_cosine_ops) WITH (m=16, ef_construction=64);
```

**各字段写入时机**：

| 字段 | 写入阶段 | 说明 |
|------|----------|------|
| segment_id ~ simhash_value | Stage 2 | 切段时写入 |
| title, content_source | Stage 2 | 切段时同步写入（LLM 生成标题或 section_title 回退） |
| segment_tags（关联表） | Stage 3 | 本体对齐 |
| embedding | Stage 6 | 段落嵌入向量回填 |
| title_vec, content_vec | Stage 6 | 标题/内容嵌入向量回填 |
| lifecycle_state 变更 | Stage 3/5 | pending_alignment（Stage 3）、superseded（Stage 5 去重） |

## 1.5 片段标签表 segment_tags

```sql
CREATE TABLE segment_tags (
    id              BIGSERIAL    PRIMARY KEY,
    segment_id      UUID         NOT NULL REFERENCES segments(segment_id),
    tag_type        VARCHAR(32)  NOT NULL,  -- canonical | semantic_role | context
    tag_value       VARCHAR(256) NOT NULL,  -- 如 'BGP', '定义', '数据中心'
    ontology_node_id VARCHAR(128),          -- 对应本体节点 ID，如 'IP.BGP'
    confidence      NUMERIC(4,3) DEFAULT 1.0,
    tagger          VARCHAR(64),            -- rule | llm | manual
    ontology_version VARCHAR(32),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_segment_tags_segment_id ON segment_tags(segment_id);
CREATE INDEX idx_segment_tags_tag_value  ON segment_tags(tag_value);
CREATE INDEX idx_segment_tags_tag_type   ON segment_tags(tag_type);
```

## 1.6 事实表 facts

```sql
CREATE TABLE facts (
    id              BIGSERIAL    PRIMARY KEY,
    fact_id         UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    subject         VARCHAR(256) NOT NULL,          -- 规范化主语，对应本体节点ID
    predicate       VARCHAR(128) NOT NULL,          -- 受控关系类型
    object          VARCHAR(256) NOT NULL,          -- 规范化宾语
    qualifier       JSONB,                          -- 限定条件，如版本、厂商、上下文
    domain          VARCHAR(128),                   -- 所属领域
    confidence      NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    lifecycle_state VARCHAR(32)  NOT NULL DEFAULT 'active',
                    -- active | deprecated | conflicted | pending_review | superseded
    merge_cluster_id UUID,                          -- 同一事实归并簇 ID
    ontology_version VARCHAR(32),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_facts_subject    ON facts(subject);
CREATE INDEX idx_facts_predicate  ON facts(predicate);
CREATE INDEX idx_facts_object     ON facts(object);
CREATE INDEX idx_facts_cluster    ON facts(merge_cluster_id);
```

## 1.7 证据表 evidence

```sql
CREATE TABLE evidence (
    id                  BIGSERIAL    PRIMARY KEY,
    evidence_id         UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id             UUID         NOT NULL REFERENCES facts(fact_id),
    source_doc_id       UUID         NOT NULL REFERENCES documents(source_doc_id),
    segment_id          UUID         REFERENCES segments(segment_id),
    exact_span          TEXT,                       -- 原文精确摘录
    span_offset_start   INTEGER,
    span_offset_end     INTEGER,
    source_rank         CHAR(1)      NOT NULL,
    extraction_method   VARCHAR(64),                -- rule | llm | manual
    evidence_score      NUMERIC(4,3) DEFAULT 0.5,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_evidence_fact_id     ON evidence(fact_id);
CREATE INDEX idx_evidence_segment_id  ON evidence(segment_id);
```

## 1.8 冲突记录表 conflict_records

> **v0.3 变更**：移入 `governance` schema。SQL 引用时需写 `governance.conflict_records`。迁移脚本：`scripts/migrations/003_governance_schema.sql`。

```sql
CREATE TABLE governance.conflict_records (
    id              BIGSERIAL    PRIMARY KEY,
    conflict_id     UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id_a       UUID         NOT NULL REFERENCES facts(fact_id),
    fact_id_b       UUID         NOT NULL REFERENCES facts(fact_id),
    conflict_type   VARCHAR(64)  NOT NULL,
                    -- contradictory_value | scope_mismatch | version_conflict | vendor_specific
    description     TEXT,
    resolution      VARCHAR(32)  DEFAULT 'open',  -- open | resolved | acknowledged
    resolved_by     VARCHAR(128),
    resolved_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.9 本体版本表 ontology_versions

> **v0.3 变更**：移入 `governance` schema。

```sql
CREATE TABLE governance.ontology_versions (
    id              SERIAL       PRIMARY KEY,
    version_tag     VARCHAR(32)  NOT NULL UNIQUE,  -- 如 'v0.1.0'
    description     TEXT,
    snapshot_uri    TEXT,                          -- 对象存储中的完整快照路径
    diff_from_prev  JSONB,                         -- 与上一版本的差异摘要
    published_by    VARCHAR(128),
    published_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status          VARCHAR(32)  NOT NULL DEFAULT 'active',
                    -- draft | active | deprecated
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.10 候选概念表 evolution_candidates

> **v0.3 变更**：移入 `governance` schema。

```sql
CREATE TABLE governance.evolution_candidates (
    id                      BIGSERIAL    PRIMARY KEY,
    candidate_id            UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_forms           TEXT[]       NOT NULL,       -- 各种原始词面形式
    normalized_form         VARCHAR(256),
    candidate_parent_id     VARCHAR(128),                -- 建议挂接的父本体节点ID
    source_count            INTEGER      NOT NULL DEFAULT 0,
    source_diversity_score  NUMERIC(4,3) DEFAULT 0.0,   -- 跨来源多样性 0~1
    temporal_stability_score NUMERIC(4,3) DEFAULT 0.0,  -- 时间稳定性 0~1
    structural_fit_score    NUMERIC(4,3) DEFAULT 0.0,   -- 结构适配性 0~1
    retrieval_gain_score    NUMERIC(4,3) DEFAULT 0.0,   -- 检索增益 0~1
    synonym_risk_score      NUMERIC(4,3) DEFAULT 0.0,   -- 同义风险 0~1（越高越危险）
    composite_score         NUMERIC(4,3) DEFAULT 0.0,   -- 综合分
    review_status           VARCHAR(32)  NOT NULL DEFAULT 'discovered',
                            -- discovered | normalized | clustered | scored
                            -- pending_review | accepted | rejected | downgraded_to_alias
    reviewer                VARCHAR(128),
    review_note             TEXT,
    first_seen_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    accepted_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.11 审核记录表 review_records

> **v0.3 变更**：移入 `governance` schema。

```sql
CREATE TABLE governance.review_records (
    id              BIGSERIAL    PRIMARY KEY,
    review_id       UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    object_type     VARCHAR(64)  NOT NULL,  -- candidate_concept | fact | conflict | ontology_change
    object_id       UUID         NOT NULL,
    action          VARCHAR(64)  NOT NULL,  -- accept | reject | defer | modify | escalate
    reviewer        VARCHAR(128) NOT NULL,
    note            TEXT,
    before_state    JSONB,
    after_state     JSONB,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

## 1.12 词汇别名表 lexicon_aliases

```sql
CREATE TABLE lexicon_aliases (
    id              BIGSERIAL    PRIMARY KEY,
    alias_id        UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_form    TEXT         NOT NULL,
    canonical_node_id VARCHAR(128) NOT NULL,  -- 映射目标本体节点ID，如 'IP.BGP'
    alias_type      VARCHAR(32)  NOT NULL,
                    -- abbreviation | full_name | vendor_term | alternate_spelling
    vendor          VARCHAR(64),              -- 若为厂商术语，填厂商名
    language        CHAR(5)      DEFAULT 'en',
    confidence      NUMERIC(4,3) DEFAULT 1.0,
    source_doc_id   UUID         REFERENCES documents(source_doc_id),
    ontology_version VARCHAR(32),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (surface_form, canonical_node_id)
);

CREATE INDEX idx_lexicon_aliases_surface_form ON lexicon_aliases(surface_form);
```

## 1.13 抽取任务表 extraction_jobs

```sql
CREATE TABLE extraction_jobs (
    id              BIGSERIAL    PRIMARY KEY,
    job_id          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    job_type        VARCHAR(64)  NOT NULL,
                    -- segmentation | tagging | ner | relation_extraction
                    -- fact_construction | dedup | embedding | ontology_alignment
    source_doc_id   UUID         REFERENCES documents(source_doc_id),
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
                    -- pending | running | done | failed
    pipeline_version VARCHAR(32),
    config_snapshot JSONB,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    error_msg       TEXT,
    stats           JSONB,       -- 处理统计，如 segments_created, facts_created
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
```

---

# 二、图数据库节点与边 Schema（Neo4j）

## 2.1 节点 Schema

### OntologyNode（本体节点）

```cypher
// 表示正式本体中的一个概念类
(:OntologyNode {
  node_id:          String,   // 唯一ID，如 "IP.BGP"，主键
  canonical_name:   String,   // 英文规范名
  display_name_zh:  String,   // 中文显示名
  domain:           String,   // 所属一级域，如 "IP / Data Communication Network"
  subdomain:        String,   // 所属子域，如 "L3 Routing"
  description:      String,
  scope_note:       String,   // 使用说明
  examples:         [String], // 典型实例举例
  maturity_level:   String,   // core | extended | experimental
  lifecycle_state:  String,   // active | deprecated
  version_introduced: String, // 首次引入版本
  version_deprecated: String,
  source_basis:     [String], // 标准依据，如 ["IETF","3GPP"]
  allowed_relations:[String]  // 允许出现的关系类型列表
})
```

### Concept（知识层概念节点）

```cypher
// 从语料中抽取的、已对齐到本体的具体概念实例
(:Concept {
  concept_id:       String,   // UUID
  canonical_name:   String,
  ontology_node_id: String,   // 对应 OntologyNode.node_id
  domain:           String,
  description:      String,
  confidence:       Float,
  lifecycle_state:  String,
  ontology_version: String,
  created_at:       DateTime
})
```

### Entity（实例节点）

```cypher
// 具体的网络对象实例，如某设备、某链路、某接口
(:Entity {
  entity_id:        String,   // UUID
  entity_name:      String,
  entity_type:      String,   // 来自本体顶层实体类，如 "Device","Interface","Alarm"
  ontology_node_id: String,
  source_doc_id:    String,
  context:          String,
  attributes:       Map,      // 实体属性键值对
  confidence:       Float,
  lifecycle_state:  String,
  created_at:       DateTime
})
```

### Fact（事实节点）

```cypher
// 规范化的知识事实三元组
(:Fact {
  fact_id:          String,   // UUID
  subject:          String,   // 规范化主语（本体节点ID或Concept ID）
  predicate:        String,   // 受控关系类型
  object:           String,   // 规范化宾语
  qualifier:        Map,      // 限定条件：{vendor, version, context, scope}
  domain:           String,
  confidence:       Float,
  lifecycle_state:  String,   // active|deprecated|conflicted|pending_review
  merge_cluster_id: String,
  ontology_version: String,
  created_at:       DateTime,
  updated_at:       DateTime
})
```

### KnowledgeSegment（知识片段节点）

```cypher
// 语义切分后的最小知识单元（仅存元数据，正文存对象存储）
(:KnowledgeSegment {
  segment_id:       String,   // UUID
  source_doc_id:    String,
  section_path:     [String],
  section_title:    String,
  segment_type:     String,
  summary:          String,   // 摘要（不存全文）
  token_count:      Integer,
  confidence:       Float,
  lifecycle_state:  String,
  embedding_ref:    String,   // 向量库中的向量 ID
  created_at:       DateTime
})
```

### SourceDocument（来源文档节点）

```cypher
// 文档在图中的代表节点，用于证据链
(:SourceDocument {
  source_doc_id:    String,   // UUID
  canonical_url:    String,
  title:            String,
  site_key:         String,
  source_rank:      String,   // S|A|B|C
  doc_type:         String,
  language:         String,
  publish_time:     DateTime,
  crawl_time:       DateTime,
  version_hint:     String,
  lifecycle_state:  String
})
```

### Evidence（证据节点）

```cypher
// 支撑某个 Fact 的原始证据
(:Evidence {
  evidence_id:      String,   // UUID
  exact_span:       String,   // 摘录原文
  source_rank:      String,
  extraction_method:String,   // rule|llm|manual
  evidence_score:   Float,
  created_at:       DateTime
})
```

### Alias（别名节点）

```cypher
// 术语别名，连接到规范本体节点
(:Alias {
  alias_id:         String,   // UUID
  surface_form:     String,   // 别名原始形式
  alias_type:       String,   // abbreviation|full_name|vendor_term|alternate_spelling
  vendor:           String,
  language:         String,
  confidence:       Float,
  ontology_version: String
})
```

### CandidateConcept（候选概念节点）

```cypher
// 尚未进入正式本体的候选概念
(:CandidateConcept {
  candidate_id:     String,   // UUID
  normalized_form:  String,
  surface_forms:    [String],
  composite_score:  Float,
  review_status:    String,
  first_seen_at:    DateTime,
  last_seen_at:     DateTime
})
```

### OntologyVersion（版本快照节点）

```cypher
(:OntologyVersion {
  version_tag:      String,   // 如 "v0.1.0"
  published_at:     DateTime,
  snapshot_uri:     String,
  status:           String    // active|deprecated
})
```

---

## 2.2 边 Schema

### 分类关系边

```cypher
// 本体节点间的子类关系
(child:OntologyNode)-[:SUBCLASS_OF {
  ontology_version: String,
  added_at:         DateTime
}]->(parent:OntologyNode)

// 概念或实体与本体节点的实例关系
(c:Concept|Entity)-[:INSTANCE_OF {
  confidence:       Float,
  ontology_version: String
}]->(n:OntologyNode)

// 组合关系
(child:OntologyNode|Concept)-[:PART_OF {
  ontology_version: String
}]->(parent:OntologyNode|Concept)
```

### 知识关系边（Fact 中的谓词投影）

```cypher
// 通用知识关系，谓词类型通过 rel_type 属性区分
// 实际建模时建议将高频关系作为具名边类型

(a:Concept|OntologyNode)-[:DEPENDS_ON {
  fact_id:    String,
  confidence: Float,
  qualifier:  Map
}]->(b:Concept|OntologyNode)

(a:Concept|OntologyNode)-[:USES {
  fact_id:    String,
  confidence: Float
}]->(b:Concept|OntologyNode)

(a:Concept|OntologyNode)-[:REQUIRES {
  fact_id:    String,
  confidence: Float,
  qualifier:  Map
}]->(b:Concept|OntologyNode)

(a:Concept|OntologyNode)-[:IMPACTS {
  fact_id:    String,
  confidence: Float,
  scope:      String
}]->(b:Concept|OntologyNode)

(a:Concept|OntologyNode)-[:CAUSES {
  fact_id:    String,
  confidence: Float
}]->(b:Concept|OntologyNode)

(a:Concept|OntologyNode)-[:ENCAPSULATES {
  fact_id:    String,
  confidence: Float
}]->(b:Concept|OntologyNode)

(a:Concept|OntologyNode)-[:ESTABLISHES {
  fact_id:    String,
  confidence: Float
}]->(b:Concept|OntologyNode)
```

### 证据链关系边

```cypher
// Fact 由 Evidence 支撑
(f:Fact)-[:SUPPORTED_BY {
  evidence_score: Float,
  added_at:       DateTime
}]->(e:Evidence)

// Evidence 来源于 SourceDocument 中的 KnowledgeSegment
(e:Evidence)-[:EXTRACTED_FROM {
  segment_id: String
}]->(s:KnowledgeSegment)

(s:KnowledgeSegment)-[:BELONGS_TO]->(d:SourceDocument)

// Fact 抽取自片段
(f:Fact)-[:DERIVED_FROM {
  extraction_method: String,
  confidence:        Float
}]->(s:KnowledgeSegment)
```

### 标签关系边

```cypher
// 片段关联本体节点（canonical tag）
(s:KnowledgeSegment)-[:TAGGED_WITH {
  tag_type:    String,  // canonical|semantic_role|context
  confidence:  Float,
  tagger:      String
}]->(n:OntologyNode)
```

### 别名关系边

```cypher
// 别名指向规范本体节点
(a:Alias)-[:ALIAS_OF {
  ontology_version: String,
  confidence:       Float
}]->(n:OntologyNode)
```

### 冲突关系边

```cypher
// 两个事实相互冲突
(f1:Fact)-[:CONTRADICTS {
  conflict_id:   String,
  conflict_type: String,
  detected_at:   DateTime
}]->(f2:Fact)
```

### 候选概念关系边

```cypher
// 候选概念建议挂接的父节点
(c:CandidateConcept)-[:CANDIDATE_CHILD_OF {
  structural_fit_score: Float,
  suggested_at:         DateTime
}]->(n:OntologyNode)
```

### 版本关系边

```cypher
// 本体节点属于某个版本
(n:OntologyNode)-[:INTRODUCED_IN]->(v:OntologyVersion)

// 版本演进
(v2:OntologyVersion)-[:SUCCEEDS {
  diff_summary: String
}]->(v1:OntologyVersion)
```

---

# 三、本体 YAML 初始文件

## 3.1 顶层关系类型定义 `ontology/top/relations.yaml`

```yaml
# ontology/top/relations.yaml
# 受控关系类型定义表
version: v0.1.0
relations:

  # 分类关系
  - id: is_a
    category: classification
    description: 继承或分类关系
    domain_hint: any
    range_hint: any
    symmetric: false
    transitive: true

  - id: subclass_of
    category: classification
    description: 子类关系
    domain_hint: OntologyNode
    range_hint: OntologyNode
    symmetric: false
    transitive: true

  - id: instance_of
    category: classification
    description: 实例归属关系
    domain_hint: Entity
    range_hint: OntologyNode
    symmetric: false
    transitive: false

  - id: part_of
    category: classification
    description: 组成关系
    domain_hint: any
    range_hint: any
    symmetric: false
    transitive: true

  # 结构关系
  - id: contains
    category: structural
    description: 包含关系
    symmetric: false
    transitive: true

  - id: connected_to
    category: structural
    description: 物理或逻辑连接
    symmetric: true
    transitive: false

  - id: hosted_on
    category: structural
    description: 承载关系
    symmetric: false
    transitive: false

  - id: mounted_on
    category: structural
    description: 安装/插接关系
    symmetric: false
    transitive: false

  - id: peers_with
    category: structural
    description: 对等互联
    symmetric: true
    transitive: false

  - id: terminates_on
    category: structural
    description: 终结于
    symmetric: false
    transitive: false

  # 协议与功能关系
  - id: uses_protocol
    category: functional
    description: 使用某协议
    symmetric: false
    transitive: false

  - id: implements
    category: functional
    description: 实现某协议或标准
    symmetric: false
    transitive: false

  - id: establishes
    category: functional
    description: 建立某会话/邻居/通道
    symmetric: false
    transitive: false

  - id: advertises
    category: functional
    description: 通告路由/信息
    symmetric: false
    transitive: false

  - id: learns
    category: functional
    description: 学习路由/信息
    symmetric: false
    transitive: false

  - id: encapsulates
    category: functional
    description: 封装
    symmetric: false
    transitive: false

  - id: forwards_via
    category: functional
    description: 转发路径/机制
    symmetric: false
    transitive: false

  - id: synchronizes_with
    category: functional
    description: 时钟同步关系
    symmetric: false
    transitive: false

  - id: authenticates
    category: functional
    description: 认证关系
    symmetric: false
    transitive: false

  - id: protects
    category: functional
    description: 保护关系
    symmetric: false
    transitive: false

  # 依赖关系
  - id: depends_on
    category: dependency
    description: 功能依赖
    symmetric: false
    transitive: true

  - id: requires
    category: dependency
    description: 必要前提条件
    symmetric: false
    transitive: false

  - id: precedes
    category: dependency
    description: 时序先于
    symmetric: false
    transitive: true

  - id: conflicts_with
    category: dependency
    description: 互斥/冲突关系
    symmetric: true
    transitive: false

  - id: constrained_by
    category: dependency
    description: 被约束
    symmetric: false
    transitive: false

  # 运维关系
  - id: raises_alarm
    category: operations
    description: 触发告警
    symmetric: false
    transitive: false

  - id: impacts
    category: operations
    description: 影响/波及
    symmetric: false
    transitive: true

  - id: causes
    category: operations
    description: 导致
    symmetric: false
    transitive: true

  - id: correlated_with
    category: operations
    description: 告警关联
    symmetric: true
    transitive: false

  - id: mitigated_by
    category: operations
    description: 被缓解/恢复
    symmetric: false
    transitive: false

  - id: monitored_by
    category: operations
    description: 被监控
    symmetric: false
    transitive: false

  - id: configured_by
    category: operations
    description: 被配置
    symmetric: false
    transitive: false

  # 证据关系（知识溯源）
  - id: supported_by
    category: evidence
    description: 被证据支撑
    symmetric: false
    transitive: false

  - id: derived_from
    category: evidence
    description: 抽取自
    symmetric: false
    transitive: false

  - id: mentioned_in
    category: evidence
    description: 被提及
    symmetric: false
    transitive: false

  - id: contradicted_by
    category: evidence
    description: 被反驳
    symmetric: false
    transitive: false
```

---

## 3.2 IP 数通子域本体 `ontology/domains/ip_network.yaml`

```yaml
# ontology/domains/ip_network.yaml
version: v0.1.0
domain: IP / Data Communication Network
domain_id: IP

nodes:

  # ── L2 交换 ─────────────────────────────────────────
  - id: IP.L2_SWITCHING
    canonical_name: L2 Switching
    display_name_zh: 二层交换
    parent_id: null
    maturity_level: core
    description: 基于 MAC 地址的数据链路层转发机制
    lifecycle_state: active
    version_introduced: v0.1.0
    source_basis: [IEEE]

  - id: IP.ETHERNET
    canonical_name: Ethernet
    display_name_zh: 以太网
    parent_id: IP.L2_SWITCHING
    maturity_level: core
    description: 基于 IEEE 802.3 的有线局域网技术
    aliases: [以太网]
    allowed_relations: [uses_protocol, encapsulates, depends_on, constrained_by]
    source_basis: [IEEE]

  - id: IP.VLAN
    canonical_name: VLAN
    display_name_zh: 虚拟局域网
    parent_id: IP.L2_SWITCHING
    maturity_level: core
    description: IEEE 802.1Q 定义的逻辑网络隔离机制
    aliases: [Virtual LAN, 802.1Q VLAN]
    allowed_relations: [encapsulates, depends_on, configured_by, contains]
    source_basis: [IEEE]

  - id: IP.QINQ
    canonical_name: QinQ
    display_name_zh: 双层 VLAN
    parent_id: IP.VLAN
    maturity_level: core
    description: IEEE 802.1ad 定义的 VLAN 堆叠机制
    aliases: [802.1ad, Stacked VLAN, Double Tagging]
    allowed_relations: [encapsulates, depends_on]
    source_basis: [IEEE]

  - id: IP.STP
    canonical_name: STP
    display_name_zh: 生成树协议
    parent_id: IP.L2_SWITCHING
    maturity_level: core
    description: IEEE 802.1D 定义的环路防止协议
    aliases: [Spanning Tree Protocol, 802.1D]
    allowed_relations: [depends_on, establishes, protects, configured_by]
    source_basis: [IEEE]

  - id: IP.RSTP
    canonical_name: RSTP
    display_name_zh: 快速生成树协议
    parent_id: IP.STP
    maturity_level: core
    description: IEEE 802.1w 定义的 STP 快速收敛增强版本
    aliases: [Rapid Spanning Tree Protocol, 802.1w]
    allowed_relations: [depends_on, establishes, protects]
    source_basis: [IEEE]

  - id: IP.MSTP
    canonical_name: MSTP
    display_name_zh: 多生成树协议
    parent_id: IP.STP
    maturity_level: core
    description: IEEE 802.1s 定义的多实例生成树协议
    aliases: [Multiple Spanning Tree Protocol, 802.1s]
    allowed_relations: [depends_on, establishes, protects, contains]
    source_basis: [IEEE]

  - id: IP.LACP
    canonical_name: LACP
    display_name_zh: 链路聚合控制协议
    parent_id: IP.L2_SWITCHING
    maturity_level: core
    description: IEEE 802.3ad 定义的链路聚合协议
    aliases: [Link Aggregation Control Protocol, 802.3ad]
    allowed_relations: [aggregates, establishes, depends_on, configured_by]
    source_basis: [IEEE]

  - id: IP.LLDP
    canonical_name: LLDP
    display_name_zh: 链路层发现协议
    parent_id: IP.L2_SWITCHING
    maturity_level: core
    description: IEEE 802.1AB 定义的邻居发现协议
    aliases: [Link Layer Discovery Protocol]
    allowed_relations: [discovers, monitored_by]
    source_basis: [IEEE]

  # ── L3 路由 ─────────────────────────────────────────
  - id: IP.L3_ROUTING
    canonical_name: L3 Routing
    display_name_zh: 三层路由
    parent_id: null
    maturity_level: core
    description: 基于 IP 地址的网络层转发机制
    lifecycle_state: active
    version_introduced: v0.1.0

  - id: IP.IPV4
    canonical_name: IPv4
    display_name_zh: IPv4
    parent_id: IP.L3_ROUTING
    maturity_level: core
    description: 第四版互联网协议，RFC 791
    aliases: [Internet Protocol version 4]
    allowed_relations: [uses_protocol, encapsulates, depends_on, constrained_by]
    source_basis: [IETF]

  - id: IP.IPV6
    canonical_name: IPv6
    display_name_zh: IPv6
    parent_id: IP.L3_ROUTING
    maturity_level: core
    description: 第六版互联网协议，RFC 8200
    aliases: [Internet Protocol version 6]
    allowed_relations: [uses_protocol, encapsulates, depends_on]
    source_basis: [IETF]

  - id: IP.ROUTING_PROTOCOL
    canonical_name: Routing Protocol
    display_name_zh: 路由协议
    parent_id: IP.L3_ROUTING
    maturity_level: core
    description: 路由协议顶层抽象类

  - id: IP.OSPF
    canonical_name: OSPF
    display_name_zh: 开放最短路径优先
    parent_id: IP.ROUTING_PROTOCOL
    maturity_level: core
    description: 基于链路状态的内部网关协议，RFC 2328（OSPFv2），RFC 5340（OSPFv3）
    aliases: [Open Shortest Path First, OSPFv2, OSPFv3]
    allowed_relations: [uses_protocol, establishes, advertises, depends_on, configured_by, constrained_by]
    source_basis: [IETF]

  - id: IP.IS_IS
    canonical_name: IS-IS
    display_name_zh: 中间系统到中间系统
    parent_id: IP.ROUTING_PROTOCOL
    maturity_level: core
    description: 基于链路状态的内部网关协议，ISO 10589 / RFC 1142
    aliases: [Intermediate System to Intermediate System, ISIS]
    allowed_relations: [uses_protocol, establishes, advertises, depends_on, configured_by]
    source_basis: [IETF, ITU-T]

  - id: IP.BGP
    canonical_name: BGP
    display_name_zh: 边界网关协议
    parent_id: IP.ROUTING_PROTOCOL
    maturity_level: core
    description: 路径矢量协议，用于 AS 间路由交换，RFC 4271
    aliases: [Border Gateway Protocol, BGP-4, eBGP, iBGP]
    allowed_relations: [uses_protocol, establishes, advertises, depends_on, configured_by, constrained_by]
    source_basis: [IETF]

  - id: IP.STATIC_ROUTE
    canonical_name: StaticRoute
    display_name_zh: 静态路由
    parent_id: IP.ROUTING_PROTOCOL
    maturity_level: core
    description: 手动配置的路由表项
    aliases: [Static Routing, 静态路由]
    allowed_relations: [configured_by, depends_on]
    source_basis: [IETF]

  - id: IP.ROUTE_POLICY
    canonical_name: RoutePolicy
    display_name_zh: 路由策略
    parent_id: IP.L3_ROUTING
    maturity_level: core
    description: 控制路由引入、过滤和属性修改的策略机制
    aliases: [Route Map, Policy-Based Routing, 路由策略]
    allowed_relations: [configured_by, constrained_by, applies_to]
    source_basis: [IETF]

  - id: IP.VRRP
    canonical_name: VRRP
    display_name_zh: 虚拟路由冗余协议
    parent_id: IP.L3_ROUTING
    maturity_level: core
    description: 提供默认网关冗余的协议，RFC 5798
    aliases: [Virtual Router Redundancy Protocol]
    allowed_relations: [establishes, protects, depends_on, configured_by]
    source_basis: [IETF]

  # ── MPLS / SR ────────────────────────────────────────
  - id: IP.MPLS_SR
    canonical_name: MPLS / SR
    display_name_zh: MPLS 与段路由
    parent_id: null
    maturity_level: core
    description: 标签交换与段路由技术体系
    lifecycle_state: active
    version_introduced: v0.1.0

  - id: IP.MPLS
    canonical_name: MPLS
    display_name_zh: 多协议标签交换
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: 基于标签的数据转发机制，RFC 3031
    aliases: [Multiprotocol Label Switching]
    allowed_relations: [uses_protocol, encapsulates, depends_on, establishes]
    source_basis: [IETF]

  - id: IP.LDP
    canonical_name: LDP
    display_name_zh: 标签分发协议
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: MPLS 标签分发协议，RFC 5036
    aliases: [Label Distribution Protocol]
    allowed_relations: [establishes, depends_on, uses_protocol]
    source_basis: [IETF]

  - id: IP.RSVP_TE
    canonical_name: RSVP-TE
    display_name_zh: 资源预留协议流量工程扩展
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: 用于 MPLS TE 路径建立的信令协议，RFC 3209
    aliases: [Resource Reservation Protocol Traffic Engineering]
    allowed_relations: [establishes, depends_on, uses_protocol, configured_by]
    source_basis: [IETF]

  - id: IP.SR_MPLS
    canonical_name: SR-MPLS
    display_name_zh: 段路由 MPLS
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: 基于 MPLS 数据平面的段路由实现
    aliases: [Segment Routing MPLS]
    allowed_relations: [depends_on, encapsulates, uses_protocol, establishes]
    source_basis: [IETF]

  - id: IP.SRV6
    canonical_name: SRv6
    display_name_zh: 段路由 IPv6
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: 基于 IPv6 数据平面的段路由实现，RFC 8986
    aliases: [Segment Routing over IPv6]
    allowed_relations: [depends_on, encapsulates, uses_protocol, establishes]
    source_basis: [IETF]

  - id: IP.LSP
    canonical_name: LSP
    display_name_zh: 标签交换路径
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: MPLS 网络中的标签交换路径
    aliases: [Label Switched Path]
    allowed_relations: [established_by, depends_on, protects]
    source_basis: [IETF]

  - id: IP.TE_POLICY
    canonical_name: TE-Policy
    display_name_zh: 流量工程策略
    parent_id: IP.MPLS_SR
    maturity_level: core
    description: SR 流量工程策略，用于路径选择与约束
    aliases: [SR Policy, SR TE Policy]
    allowed_relations: [configured_by, depends_on, forwards_via]
    source_basis: [IETF]

  # ── VPN / Overlay ────────────────────────────────────
  - id: IP.VPN_OVERLAY
    canonical_name: VPN / Overlay
    display_name_zh: VPN 与覆盖网络
    parent_id: null
    maturity_level: core
    description: 基于隧道与虚拟化的网络隔离和叠加技术
    lifecycle_state: active
    version_introduced: v0.1.0

  - id: IP.VRF
    canonical_name: VRF
    display_name_zh: 虚拟路由转发实例
    parent_id: IP.VPN_OVERLAY
    maturity_level: core
    description: 路由器内的虚拟路由实例，用于三层隔离
    aliases: [Virtual Routing and Forwarding]
    allowed_relations: [contains, depends_on, configured_by, isolates]
    source_basis: [IETF]

  - id: IP.L3VPN
    canonical_name: L3VPN
    display_name_zh: 三层 VPN
    parent_id: IP.VPN_OVERLAY
    maturity_level: core
    description: 基于 MPLS/BGP 的三层虚拟专用网，RFC 4364
    aliases: [BGP/MPLS IP VPN]
    allowed_relations: [depends_on, uses_protocol, contains, configured_by]
    source_basis: [IETF]

  - id: IP.EVPN
    canonical_name: EVPN
    display_name_zh: 以太网 VPN
    parent_id: IP.VPN_OVERLAY
    maturity_level: core
    description: 基于 BGP 控制平面的以太网 VPN，RFC 7432
    aliases: [Ethernet VPN, BGP EVPN]
    allowed_relations: [uses_protocol, depends_on, encapsulates, establishes, configured_by]
    source_basis: [IETF]

  - id: IP.VXLAN
    canonical_name: VXLAN
    display_name_zh: 虚拟可扩展局域网
    parent_id: IP.VPN_OVERLAY
    maturity_level: core
    description: 基于 UDP 的 Overlay 封装协议，RFC 7348
    aliases: [Virtual Extensible LAN]
    allowed_relations: [encapsulates, depends_on, established_by, configured_by]
    source_basis: [IETF]

  - id: IP.EVPN_VXLAN
    canonical_name: EVPN-VXLAN
    display_name_zh: EVPN over VXLAN
    parent_id: IP.VPN_OVERLAY
    maturity_level: core
    description: 以 EVPN 为控制平面、VXLAN 为数据平面的组合方案
    aliases: [EVPN over VXLAN]
    allowed_relations: [depends_on, uses_protocol, encapsulates, contains]
    source_basis: [IETF]

  - id: IP.VTEP
    canonical_name: VTEP
    display_name_zh: VXLAN 隧道端点
    parent_id: IP.VXLAN
    maturity_level: core
    description: 执行 VXLAN 封装/解封装的逻辑实体
    aliases: [VXLAN Tunnel Endpoint]
    allowed_relations: [encapsulates, peers_with, depends_on]
    source_basis: [IETF]

  # ── QoS ─────────────────────────────────────────────
  - id: IP.QOS
    canonical_name: QoS
    display_name_zh: 服务质量
    parent_id: null
    maturity_level: core
    description: 对网络流量进行分类、调度和管控的机制体系
    lifecycle_state: active
    version_introduced: v0.1.0

  - id: IP.QOS_CLASSIFIER
    canonical_name: TrafficClassifier
    display_name_zh: 流量分类器
    parent_id: IP.QOS
    maturity_level: core
    description: 根据报文特征对流量进行分类的配置对象
    aliases: [Classifier, Traffic Classifier]
    allowed_relations: [configured_by, depends_on, applies_to]
    source_basis: [IETF]

  - id: IP.QOS_SCHEDULER
    canonical_name: Scheduler
    display_name_zh: 调度器
    parent_id: IP.QOS
    maturity_level: core
    description: 控制出队顺序和带宽分配的调度机制
    aliases: [WFQ, CBWFQ, PQ, DRR]
    allowed_relations: [configured_by, depends_on, contains]
    source_basis: [IETF]

  - id: IP.QOS_POLICER
    canonical_name: Policer
    display_name_zh: 流量监管器
    parent_id: IP.QOS
    maturity_level: core
    description: 对流量速率进行限制并丢弃超限报文的机制
    aliases: [Traffic Policing, CAR]
    allowed_relations: [configured_by, constrained_by, depends_on]
    source_basis: [IETF]

  - id: IP.QOS_SHAPER
    canonical_name: Shaper
    display_name_zh: 流量整形器
    parent_id: IP.QOS
    maturity_level: core
    description: 通过缓冲对流量速率进行平滑的机制
    aliases: [Traffic Shaping]
    allowed_relations: [configured_by, depends_on]
    source_basis: [IETF]

  # ── Security / Control ───────────────────────────────
  - id: IP.SECURITY_CTRL
    canonical_name: Security / Control
    display_name_zh: 安全与控制
    parent_id: null
    maturity_level: core
    description: IP 层安全过滤与访问控制机制
    lifecycle_state: active
    version_introduced: v0.1.0

  - id: IP.ACL
    canonical_name: ACL
    display_name_zh: 访问控制列表
    parent_id: IP.SECURITY_CTRL
    maturity_level: core
    description: 基于报文字段进行过滤的访问控制规则集
    aliases: [Access Control List, 访问控制列表]
    allowed_relations: [configured_by, constrained_by, filters, depends_on]
    source_basis: [IETF]

  - id: IP.NAT
    canonical_name: NAT
    display_name_zh: 网络地址转换
    parent_id: IP.SECURITY_CTRL
    maturity_level: core
    description: 地址和端口转换机制，RFC 3022
    aliases: [Network Address Translation, NAPT, PAT]
    allowed_relations: [configured_by, depends_on, conflicts_with]
    source_basis: [IETF]

  - id: IP.IPSEC
    canonical_name: IPsec
    display_name_zh: IP 安全协议
    parent_id: IP.SECURITY_CTRL
    maturity_level: core
    description: 提供 IP 层加密和认证的协议套件，RFC 4301
    aliases: [IP Security, IPSec]
    allowed_relations: [encapsulates, authenticates, depends_on, established_by, configured_by]
    source_basis: [IETF]

  - id: IP.BFD
    canonical_name: BFD
    display_name_zh: 双向转发检测
    parent_id: IP.SECURITY_CTRL
    maturity_level: core
    description: 快速链路/路径故障检测协议，RFC 5880
    aliases: [Bidirectional Forwarding Detection]
    allowed_relations: [depends_on, monitors, established_by, triggers]
    source_basis: [IETF]

  - id: IP.GRE
    canonical_name: GRE
    display_name_zh: 通用路由封装
    parent_id: IP.VPN_OVERLAY
    maturity_level: core
    description: 通用封装隧道协议，RFC 2784
    aliases: [Generic Routing Encapsulation]
    allowed_relations: [encapsulates, depends_on, established_by]
    source_basis: [IETF]

  # ── OAM / Monitoring ─────────────────────────────────
  - id: IP.OAM_MONITORING
    canonical_name: OAM / Monitoring
    display_name_zh: 运维监控
    parent_id: null
    maturity_level: core
    description: IP 网络的运维管理和可观测性机制
    lifecycle_state: active
    version_introduced: v0.1.0

  - id: IP.TELEMETRY
    canonical_name: Telemetry
    display_name_zh: 遥测
    parent_id: IP.OAM_MONITORING
    maturity_level: core
    description: 设备主动推送性能和状态数据的机制
    aliases: [gRPC Telemetry, Model-driven Telemetry, gNMI Telemetry]
    allowed_relations: [collects, monitored_by, depends_on]
    source_basis: [IETF]

  - id: IP.SYSLOG
    canonical_name: Syslog
    display_name_zh: 系统日志
    parent_id: IP.OAM_MONITORING
    maturity_level: core
    description: 系统和网络设备日志消息协议，RFC 5424
    aliases: [System Log]
    allowed_relations: [collects, reports, depends_on]
    source_basis: [IETF]

  - id: IP.NETCONF
    canonical_name: NETCONF
    display_name_zh: 网络配置协议
    parent_id: IP.OAM_MONITORING
    maturity_level: core
    description: 基于 XML/YANG 的网络配置管理协议，RFC 6241
    aliases: [Network Configuration Protocol]
    allowed_relations: [configures, depends_on, uses_protocol]
    source_basis: [IETF]

  - id: IP.YANG
    canonical_name: YANG
    display_name_zh: YANG 数据模型
    parent_id: IP.OAM_MONITORING
    maturity_level: core
    description: 用于 NETCONF/RESTCONF 的数据建模语言，RFC 7950
    aliases: [YANG Data Model]
    allowed_relations: [describes, depends_on, referenced_by]
    source_basis: [IETF]
```

---

## 3.3 词汇别名文件 `ontology/lexicon/aliases.yaml`（IP 子域示例）

```yaml
# ontology/lexicon/aliases.yaml
version: v0.1.0

aliases:
  # BGP 别名
  - surface_form: Border Gateway Protocol
    canonical_node_id: IP.BGP
    alias_type: full_name
    language: en

  - surface_form: BGP-4
    canonical_node_id: IP.BGP
    alias_type: abbreviation
    language: en

  - surface_form: eBGP
    canonical_node_id: IP.BGP
    alias_type: alternate_spelling
    language: en

  - surface_form: iBGP
    canonical_node_id: IP.BGP
    alias_type: alternate_spelling
    language: en

  - surface_form: 边界网关协议
    canonical_node_id: IP.BGP
    alias_type: full_name
    language: zh

  # OSPF 别名
  - surface_form: Open Shortest Path First
    canonical_node_id: IP.OSPF
    alias_type: full_name
    language: en

  - surface_form: OSPFv2
    canonical_node_id: IP.OSPF
    alias_type: abbreviation
    language: en

  - surface_form: OSPFv3
    canonical_node_id: IP.OSPF
    alias_type: abbreviation
    language: en

  - surface_form: 开放最短路径优先
    canonical_node_id: IP.OSPF
    alias_type: full_name
    language: zh

  # IS-IS 别名
  - surface_form: ISIS
    canonical_node_id: IP.IS_IS
    alias_type: abbreviation
    language: en

  - surface_form: Intermediate System to Intermediate System
    canonical_node_id: IP.IS_IS
    alias_type: full_name
    language: en

  # MPLS 别名
  - surface_form: Multiprotocol Label Switching
    canonical_node_id: IP.MPLS
    alias_type: full_name
    language: en

  - surface_form: 多协议标签交换
    canonical_node_id: IP.MPLS
    alias_type: full_name
    language: zh

  # EVPN 别名
  - surface_form: Ethernet VPN
    canonical_node_id: IP.EVPN
    alias_type: full_name
    language: en

  - surface_form: BGP EVPN
    canonical_node_id: IP.EVPN
    alias_type: alternate_spelling
    language: en

  - surface_form: 以太网VPN
    canonical_node_id: IP.EVPN
    alias_type: full_name
    language: zh

  # VXLAN 别名
  - surface_form: Virtual Extensible LAN
    canonical_node_id: IP.VXLAN
    alias_type: full_name
    language: en

  # SRv6 别名
  - surface_form: Segment Routing over IPv6
    canonical_node_id: IP.SRV6
    alias_type: full_name
    language: en

  - surface_form: SRv6 BE
    canonical_node_id: IP.SRV6
    alias_type: alternate_spelling
    language: en

  # ACL 别名
  - surface_form: Access Control List
    canonical_node_id: IP.ACL
    alias_type: full_name
    language: en

  - surface_form: 访问控制列表
    canonical_node_id: IP.ACL
    alias_type: full_name
    language: zh

  # VRRP 别名
  - surface_form: Virtual Router Redundancy Protocol
    canonical_node_id: IP.VRRP
    alias_type: full_name
    language: en

  # 厂商术语示例
  - surface_form: Enhanced OSPF
    canonical_node_id: IP.OSPF
    alias_type: vendor_term
    vendor: Cisco
    language: en

  - surface_form: IP Fast Reroute
    canonical_node_id: IP.OSPF
    alias_type: vendor_term
    vendor: Cisco
    language: en

  - surface_form: IP FRR
    canonical_node_id: IP.OSPF
    alias_type: vendor_term
    vendor: multi
    language: en
```

---

## 3.4 演化策略配置 `ontology/governance/evolution_policy.yaml`

```yaml
# ontology/governance/evolution_policy.yaml
version: v0.1.0

layers:
  core_ontology:
    change_allowed: manual_only
    reviewers_required: 2
    change_quota_per_cycle: 5
    rollback_supported: true
    description: 顶层类、关系类型、核心域骨架

  domain_ontology:
    change_allowed: semi_auto
    reviewers_required: 1
    change_quota_per_cycle: 20
    rollback_supported: true
    description: 子域概念、子域关系补充

  lexicon_layer:
    change_allowed: auto_with_threshold
    min_confidence: 0.80
    min_source_count: 2
    rollback_supported: true
    description: 别名、缩写、厂商术语

candidate_admission:
  min_source_count: 3
  min_source_diversity: 0.6      # 至少来自 3 个不同站点
  min_temporal_stability: 0.7    # 在至少 2 个采集周期出现
  min_structural_fit: 0.65       # 能清晰挂接父节点
  min_composite_score: 0.65
  synonym_risk_max: 0.4          # 同义风险不超过 0.4
  require_human_review: true     # 所有候选都需人工最终确认

anti_drift:
  block_parentless_candidate: true
  block_alias_promotion_to_concept: true
  require_impact_analysis_before_publish: true
  max_core_changes_per_release: 5
```

---

# 四、语义算子 API 设计

## 4.1 设计原则

- 所有算子通过 **RESTful HTTP API** 暴露，JSON 格式
- 基础 URL：`/api/v1/semantic/`
- 所有请求包含 `ontology_version` 参数（不传则使用当前 active 版本）
- 所有响应包含 `meta.latency_ms`、`meta.ontology_version`
- 算子设计为**无副作用**（只读），写操作通过治理 API 另行暴露

---

## 4.2 基础算子

### `GET /api/v1/semantic/lookup`

术语查询：解析术语为本体节点，返回定义、别名、相关证据。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `term` | string | 是 | 查询术语，可为别名/缩写/原始词面 |
| `scope` | string | 否 | 限定搜索域，如 `IP`、`OPTICAL` |
| `lang` | string | 否 | 语言，`en`/`zh`，默认 `en` |
| `ontology_version` | string | 否 | 指定本体版本 |
| `include_evidence` | bool | 否 | 是否返回关联证据，默认 false |
| `max_evidence` | int | 否 | 最多返回 N 条证据，默认 3 |

**响应示例**

```json
{
  "meta": {
    "ontology_version": "v0.1.0",
    "latency_ms": 12
  },
  "result": {
    "matched_node": {
      "node_id": "IP.BGP",
      "canonical_name": "BGP",
      "display_name_zh": "边界网关协议",
      "domain": "IP / Data Communication Network",
      "description": "路径矢量协议，用于AS间路由交换，RFC 4271",
      "maturity_level": "core",
      "lifecycle_state": "active"
    },
    "match_type": "alias",           // exact | alias | fuzzy | not_found
    "input_surface_form": "Border Gateway Protocol",
    "aliases": ["Border Gateway Protocol", "BGP-4", "eBGP", "iBGP"],
    "allowed_relations": ["uses_protocol", "establishes", "advertises", "depends_on"],
    "evidence": [
      {
        "evidence_id": "...",
        "exact_span": "BGP is a path-vector routing protocol...",
        "source_url": "https://www.ietf.org/rfc/rfc4271",
        "source_rank": "S",
        "evidence_score": 0.98
      }
    ]
  }
}
```

---

### `GET /api/v1/semantic/resolve`

别名解析：将缩写/别名/厂商术语映射为规范本体节点。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `alias` | string | 是 | 待解析的别名/缩写 |
| `scope` | string | 否 | 限定搜索域 |
| `vendor` | string | 否 | 厂商上下文 |

**响应示例**

```json
{
  "result": {
    "input": "BGP-4",
    "resolved": {
      "node_id": "IP.BGP",
      "canonical_name": "BGP",
      "confidence": 0.99,
      "alias_type": "abbreviation"
    },
    "alternatives": []
  }
}
```

---

### `GET /api/v1/semantic/expand`

概念邻域扩展：围绕某个本体节点获取其关联知识图谱邻域。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `node_id` | string | 是 | 本体节点 ID，如 `IP.BGP` |
| `relation_types` | string[] | 否 | 过滤关系类型，不传则返回所有 |
| `depth` | int | 否 | 扩展深度，默认 1，最大 3 |
| `min_confidence` | float | 否 | 最低可信度过滤，默认 0.5 |
| `include_facts` | bool | 否 | 是否包含关联事实，默认 true |
| `include_segments` | bool | 否 | 是否包含关联知识片段引用，默认 false |

**响应示例**

```json
{
  "result": {
    "center": { "node_id": "IP.BGP", "canonical_name": "BGP" },
    "neighbors": [
      {
        "node_id": "IP.ROUTING_PROTOCOL",
        "canonical_name": "Routing Protocol",
        "relation": "subclass_of",
        "direction": "outbound",
        "confidence": 1.0
      },
      {
        "node_id": "IP.EVPN",
        "canonical_name": "EVPN",
        "relation": "uses_protocol",
        "direction": "inbound",
        "confidence": 0.97
      }
    ],
    "facts": [
      {
        "fact_id": "...",
        "subject": "IP.BGP",
        "predicate": "uses_protocol",
        "object": "TCP",
        "confidence": 0.99
      }
    ]
  }
}
```

---

### `POST /api/v1/semantic/filter`

过滤器：对知识对象集合按条件过滤。

**请求体**

```json
{
  "object_type": "fact",         // fact | segment | concept
  "filters": {
    "source_rank": ["S", "A"],
    "min_confidence": 0.7,
    "domain": "IP / Data Communication Network",
    "lifecycle_state": "active",
    "tags": ["BGP", "路由协议"],
    "after_date": "2023-01-01",
    "vendor": "Cisco"
  },
  "sort_by": "confidence",
  "sort_order": "desc",
  "page": 1,
  "page_size": 20
}
```

---

## 4.3 关系算子

### `GET /api/v1/semantic/path`

路径推断：发现两个概念间的语义路径。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `start_node_id` | string | 是 | 起始节点 |
| `end_node_id` | string | 是 | 目标节点 |
| `relation_policy` | string | 否 | `all`/`dependency`/`causal`，默认 `all` |
| `max_hops` | int | 否 | 最大跳数，默认 5 |
| `min_confidence` | float | 否 | 路径上最低边可信度 |

**响应示例**

```json
{
  "result": {
    "paths": [
      {
        "hops": 2,
        "path": [
          {"node_id": "IP.EVPN", "relation": "uses_protocol", "confidence": 0.97},
          {"node_id": "IP.BGP",  "relation": "depends_on",    "confidence": 0.95},
          {"node_id": "IP.OSPF", "relation": null,             "confidence": null}
        ],
        "path_confidence": 0.92
      }
    ],
    "path_count": 1
  }
}
```

---

### `GET /api/v1/semantic/dependency_closure`

依赖闭包：求取某个协议/机制/配置对象的完整依赖树。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `node_id` | string | 是 | 起始节点 |
| `relation_types` | string[] | 否 | 默认 `[depends_on, requires]` |
| `max_depth` | int | 否 | 默认 6 |
| `include_optional` | bool | 否 | 是否包含可选依赖，默认 false |

**响应示例**

```json
{
  "result": {
    "root": "IP.EVPN_VXLAN",
    "closure": {
      "IP.EVPN_VXLAN": {
        "depends_on": ["IP.EVPN", "IP.VXLAN"],
        "requires": ["IP.BGP", "IP.VTEP"]
      },
      "IP.EVPN": {
        "depends_on": ["IP.BGP"],
        "requires": ["IP.MPLS"]
      },
      "IP.BGP": {
        "depends_on": ["TCP"]
      }
    },
    "total_nodes": 6
  }
}
```

---

### `POST /api/v1/semantic/impact_propagate`

影响传播：从故障或事件节点出发，计算影响扩散范围。

**请求体**

```json
{
  "event_node_id": "IP.BGP",
  "event_type": "fault",          // fault | config_change | alarm
  "relation_policy": "causal",    // all | causal | service
  "max_depth": 4,
  "min_confidence": 0.6,
  "context": {
    "vendor": "Cisco",
    "deployment": "数据中心"
  }
}
```

**响应示例**

```json
{
  "result": {
    "event": { "node_id": "IP.BGP", "event_type": "fault" },
    "impact_tree": [
      {
        "node_id": "IP.EVPN",
        "impact_type": "disrupted",
        "confidence": 0.91,
        "via_relation": "uses_protocol",
        "depth": 1
      },
      {
        "node_id": "IP.L3VPN",
        "impact_type": "disrupted",
        "confidence": 0.85,
        "via_relation": "depends_on",
        "depth": 2
      }
    ],
    "total_impacted": 2
  }
}
```

---

## 4.4 证据算子

### `GET /api/v1/semantic/evidence_rank`

证据排序：对支撑同一事实的证据按质量排序。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `fact_id` | string | 是 | 事实 UUID |
| `rank_by` | string | 否 | `source_rank`/`evidence_score`/`recency`，默认 `evidence_score` |
| `max_results` | int | 否 | 默认 10 |

---

### `POST /api/v1/semantic/fact_merge`

事实融合：将候选重复事实归并为规范事实（写操作，需权限）。

**请求体**

```json
{
  "fact_ids": ["uuid-1", "uuid-2", "uuid-3"],
  "merge_strategy": "highest_confidence",  // highest_confidence | union_evidence | manual
  "canonical_fact": {
    "subject": "IP.BGP",
    "predicate": "uses_protocol",
    "object": "TCP"
  }
}
```

---

### `GET /api/v1/semantic/conflict_detect`

冲突检测：检测同一主题下的冲突知识。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `topic_node_id` | string | 是 | 主题本体节点 |
| `predicate` | string | 否 | 限定关系类型 |
| `min_confidence` | float | 否 | 默认 0.5 |

**响应示例**

```json
{
  "result": {
    "conflicts": [
      {
        "conflict_id": "...",
        "fact_a": { "fact_id": "...", "subject": "IP.OSPF", "predicate": "requires", "object": "IP.IPv4" },
        "fact_b": { "fact_id": "...", "subject": "IP.OSPF", "predicate": "requires", "object": "IP.IPv6" },
        "conflict_type": "contradictory_value",
        "resolution": "open",
        "note": "OSPFv2 only supports IPv4; OSPFv3 supports both IPv4 and IPv6"
      }
    ],
    "total": 1
  }
}
```

---

## 4.5 演化算子

### `GET /api/v1/semantic/candidate_discover`

候选概念发现：在指定时间窗口内发现潜在新概念。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `window_days` | int | 是 | 发现时间窗口（天） |
| `min_frequency` | int | 否 | 最低出现频次，默认 5 |
| `domain` | string | 否 | 限定搜索域 |
| `min_source_count` | int | 否 | 最少来源数，默认 2 |

---

### `GET /api/v1/semantic/attach_score`

挂接评分：评估候选概念与各父节点的结构适配性。

**请求参数**

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `candidate_id` | string | 是 | 候选概念 UUID |
| `candidate_parent_ids` | string[] | 否 | 候选父节点列表（不传则自动推荐） |

**响应示例**

```json
{
  "result": {
    "candidate_id": "...",
    "normalized_form": "SRv6 Policy",
    "recommendations": [
      { "parent_node_id": "IP.SRV6", "structural_fit_score": 0.88, "reason": "SRv6 Policy 是 SRv6 的核心机制" },
      { "parent_node_id": "IP.TE_POLICY", "structural_fit_score": 0.72, "reason": "语义上也属于 TE 策略范畴" }
    ]
  }
}
```

---

### `POST /api/v1/semantic/evolution_gate`

演化门控：判断候选概念是否满足进入审核流程的阈值。

**请求体**

```json
{
  "candidate_id": "..."
}
```

**响应示例**

```json
{
  "result": {
    "candidate_id": "...",
    "gate_passed": true,
    "scores": {
      "source_count": 5,
      "source_diversity_score": 0.75,
      "temporal_stability_score": 0.82,
      "structural_fit_score": 0.88,
      "retrieval_gain_score": 0.70,
      "synonym_risk_score": 0.15,
      "composite_score": 0.78
    },
    "blocking_reasons": [],
    "action": "submit_to_review"
  }
}
```

---

# 五、抽取与去重流水线详细规则说明

## 5.1 阶段 1：采集与标准化

### 规则 C1 — robots.txt 遵守

- 每次对站点发起采集前，先检查 `robots.txt` 缓存（TTL 24h）
- 禁止采集 `Disallow` 规则覆盖的路径
- 若站点未提供 `robots.txt`，默认允许采集

### 规则 C2 — 速率限制

- 每个站点最高 `rate_limit_rps`（来自 source_registry）
- 两次请求之间强制插入随机 jitter：[0.5s, 2.0s]
- 若 HTTP 429 / 503，退避策略：指数退避，最大等待 5min，最多重试 3 次

### 规则 C3 — 内容哈希去重（采集层）

- 抓取成功后计算原始 HTML 的 SHA-256 → `content_hash`
- 若 `content_hash` 与数据库中已存在记录相同，标记为 `deduped`，跳过后续处理
- 同时检查 canonical URL 是否已存在

### 规则 C4 — 正文抽取优先级

按以下顺序尝试正文抽取策略：
1. 站点白名单中配置的 CSS selector 规则（最优先）
2. trafilatura 自动抽取
3. readability-lxml
4. 最后降级：提取 `<body>` 全文并标记置信度低

去模板后正文长度 < 200 tokens → 标记为 `low_quality_page`，不进入后续加工

### 规则 C5 — 文档类型识别

按以下信号组合判断 `doc_type`：

| 信号 | 说明 |
|---|---|
| URL 包含 `/rfc/` | → `spec` |
| 标题匹配 `RFC \d+` | → `spec` |
| URL 包含 `/config-guide/` | → `vendor_doc` |
| 页面 H1 包含 "Configuration Guide" | → `vendor_doc` |
| URL 包含 `/troubleshoot/` | → `vendor_doc` |
| 正文中 FAQ 模式密度 > 30% | → `faq` |
| 含代码块 > 3 个 | → `tutorial` |
| 文件后缀 `.pdf` | → `pdf` |
| 其他 | → `tech_article` |

---

## 5.2 阶段 2：语义切分

### 规则 S1 — 结构切分优先

按文档结构边界切分，优先级从高到低：

1. H1 / H2 / H3 标题（产生独立 segment）
2. 表格（每张表格 → 单独 segment，类型 `table`）
3. 代码块（每个 ` ``` ` 包围的块 → `code` segment）
4. 命令行块（以 `#` / `>` 开头的行块 → `config` segment）
5. 有序/无序列表（若列表项 > 100 tokens → 作为独立 segment）

### 规则 S2 — 语义切分（段内细分）

对于 `segment_type == unknown` 且 token_count > 300 的段落，启动语义角色分类：

1. 使用规则匹配模式（优先）：
   - 包含 "is defined as" / "refers to" / "is a" → `definition`
   - 包含 "works by" / "mechanism" / "algorithm" → `mechanism`
   - 包含 "must" / "shall" / "required" / "limitation" → `constraint`
   - 包含 "configure" / "set" / "enable" → `config`
   - 包含 "fault" / "failure" / "error" / "down" → `fault`
   - 包含 "troubleshoot" / "debug" / "verify" → `troubleshooting`
   - 包含 "best practice" / "recommendation" → `best_practice`
   - 包含 "performance" / "throughput" / "latency" → `performance`
   - 包含 "compared to" / "versus" / "difference" → `comparison`

2. 若规则未命中，使用 LLM 做语义角色分类（触发阈值：token_count > 400）
3. 分类结果置信度 < 0.7 时，保留为 `unknown`，不强行分类

### 规则 S3 — 片段长度控制

| 场景 | 处理规则 |
|---|---|
| token_count < 30 | 合并到前一个 segment，不单独成片 |
| 30 ≤ token_count ≤ 512 | 直接入库 |
| 512 < token_count ≤ 1024 | 尝试按规则 S2 细分；若无法细分，整体保留并标注 `oversized` |
| token_count > 1024 | 必须细分；若仍超过，按 512 token 滑动窗口切分，overlap 64 token |

### 规则 S4 — Section Path 保留

每个 segment 必须记录完整的 section_path（章节路径），用于：
- 上下文重建
- 检索结果展示
- 证据追溯

---

## 5.3 阶段 3：语义识别与本体对齐

### 规则 A1 — 术语识别策略

按以下顺序执行：
1. **词典精确匹配**：对 `lexicon_aliases` 表中的所有 `surface_form` 做正向最大匹配
2. **大小写不敏感匹配**：处理全大写/首字母大写变体
3. **缩写展开**：匹配缩写词表，如 `BGP` → `Border Gateway Protocol` → `IP.BGP`
4. **NER 模型**：使用训练好的领域 NER 模型识别未命中的技术术语
5. **LLM 增强**（仅对 S/A 级来源触发）：对 NER 未识别的高信噪比段落做 LLM 术语抽取

### 规则 A2 — 本体对齐决策树

```
term 识别出来后：
  ├── 精确命中 OntologyNode.canonical_name → 对齐，confidence = 1.0
  ├── 命中 lexicon_aliases.surface_form → 对齐到 canonical_node_id，confidence = alias.confidence
  ├── 相似度 > 0.85（向量相似度）→ 对齐，confidence = similarity_score，标注为 fuzzy_match
  └── 相似度 ≤ 0.85 → 进入候选概念池（CandidateConcept），不对齐到正式本体
```

### 规则 A3 — 禁止无约束新增本体节点

- 任何自动化流程**不得**直接向 OntologyNode 写入新节点
- 新候选只能写入 `evolution_candidates` 表和图数据库中的 `CandidateConcept` 节点
- 正式入本体必须经过 `evolution_gate` 评分 + 人工审核

### 规则 A4 — 厂商术语归一策略

- 若 surface_form 命中 `vendor_term` 类型别名，归一到对应 canonical_node_id
- 同时保留厂商维度标注（`qualifier.vendor = "Cisco"`）
- 厂商私有特性不能覆盖标准概念的定义域

### 规则 A5 — Canonical Tag 最低要求

- 每个 segment 必须有**至少 1 个** canonical tag（对应本体节点）
- 若对齐完全失败（所有术语均进入候选池），该 segment 标记 `pending_alignment`，不计入主知识库
- semantic_role tag 和 context tag 可以为空，但建议尽量填充

---

## 5.4 阶段 4：关系抽取与事实构造

### 规则 R1 — 关系抽取约束

所有抽取出的关系必须满足：
1. 谓词必须属于 `ontology/top/relations.yaml` 定义的受控集合
2. 主语和宾语必须能映射到本体节点（允许 Concept 层）
3. 谓词对应关系的 `domain_hint` / `range_hint` 约束需检查
4. 违约关系进入候选事实池，标注 `constraint_violation`，不入主库

### 规则 R2 — 关系抽取方法选择

| 来源等级 | 主要方法 |
|---|---|
| S 级 | 规则模板 + LLM 验证 |
| A 级 | 规则模板 + LLM 抽取 |
| B 级 | LLM 抽取 + 规则过滤 |
| C 级 | 规则模板为主，LLM 辅助，低置信度 |

### 规则 R3 — 事实构造规范

事实三元组生成规则：
- `subject`：必须使用 OntologyNode.node_id 或 Concept.concept_id
- `predicate`：必须来自受控关系集合
- `object`：优先使用 OntologyNode.node_id；若为字面量值（如端口号），保留字符串并加 `qualifier.literal_type`
- `qualifier`：记录所有限定条件，包括 `vendor`、`version`、`context`、`scope`

### 规则 R4 — 可信度计算

```
fact.confidence = (
    w1 * source_authority_score(source_rank)  +
    w2 * extraction_method_score(method)      +
    w3 * ontology_fit_score                   +
    w4 * cross_source_consistency_score       +
    w5 * temporal_validity_score
)

参考权重：w1=0.30, w2=0.20, w3=0.20, w4=0.20, w5=0.10

source_authority_score：S=1.0, A=0.85, B=0.65, C=0.40
extraction_method_score：manual=1.0, rule=0.85, llm=0.70
```

---

## 5.5 阶段 5：去重与融合

### 规则 D1 — 页面级去重（采集层）

去重判断顺序：
1. canonical URL 完全一致 → 直接标记 `deduped`
2. content_hash（原始 HTML SHA-256）一致 → 标记 `deduped`
3. normalized_hash（去模板正文 SHA-256）一致 → 标记为 `duplicate`，保留来源等级更高的版本

### 规则 D2 — 段落级去重

1. **SimHash 快速过滤**：计算 normalized_text 的 64-bit SimHash
   - 汉明距离 ≤ 3 → 视为近似重复候选
   - 汉明距离 ≤ 1 → 视为强重复

2. **精确比对**：对近似重复候选做 token 级 Jaccard 相似度计算
   - Jaccard > 0.85 → 确认为重复，保留来源等级更高的 segment

3. **联合签名**：对 section_title + normalized_text[:200] 做 SHA-256，快速发现结构性重复

4. 去重后处理：
   - 保留最高 source_rank 的 segment 作为主 segment
   - 其余 segment 标记 `superseded`，但保留其证据贡献能力（仍可作为 Evidence）

### 规则 D3 — 事实级去重

**判断为重复的条件**（满足其一即触发）：

```
条件 A：subject + predicate + object 完全一致（规范化后）
条件 B：subject + predicate 一致，object 语义等价
        （通过 lexicon_aliases 归一后相同）
条件 C：subject + predicate + object 一致，qualifier 不同
        → 视为同一事实的不同适用上下文，合并并在 qualifier 中记录差异
```

**融合策略**：
1. 创建 `merge_cluster_id` 将重复事实归组
2. 从中选取 confidence 最高的事实作为规范事实
3. 将所有来源的 Evidence 全部关联到规范事实
4. 原始重复事实标记 `superseded`，但保留不删除

### 规则 D4 — 冲突检测与处理

**冲突触发条件**：
- 同 subject + predicate，object 语义**不等价**且**互斥**（如 `requires IPv4` vs `requires IPv6`）

**冲突处理策略**：
1. **不强行合并**：冲突双方均保留为 `conflicted` 状态
2. **增加上下文标注**：检查是否版本差异（OSPFv2 vs OSPFv3）、厂商差异（Cisco vs Huawei）、场景差异
3. **自动解析**：若冲突双方 qualifier 中有明确不同的 `vendor`/`version`/`context`，自动降级为非冲突（contextual difference）
4. **人工审核队列**：真正语义冲突（上下文相同但结论不同）进入人工审核
5. **来源等级仲裁**：S 级来源的事实优先级高于 A/B/C；但不自动删除低等级冲突方，保留为辅助证据

### 规则 D5 — 归一化预处理（去重前置）

去重执行前必须先做：
1. 小写化（英文）
2. 全半角统一（中文）
3. 多余空白字符压缩
4. 标点归一（中英文标点统一为英文）
5. 常见缩写展开（通过 lexicon_aliases 词典）

---

## 5.6 阶段 6：入库规则

### 规则 I1 — 入库门控阈值

| 对象类型 | 入主库最低条件 |
|---|---|
| Segment | 至少 1 个 canonical tag，confidence ≥ 0.5 |
| Fact | 至少 1 条 Evidence，confidence ≥ 0.5 |
| Concept | 已对齐到本体节点，confidence ≥ 0.6 |
| Alias | 来自 S/A 级来源，或人工确认 |
| CandidateConcept | 无硬性门控，全量入候选表 |

### 规则 I2 — 事务一致性

- Fact 与其关联的 Evidence 必须同时入库（原子操作）
- Segment 与其 segment_tags 必须同时入库
- 图数据库与 PostgreSQL 的写入顺序：先写 PostgreSQL 元数据，再写图数据库节点/边，再写向量索引

### 规则 I3 — 版本标注

所有入库对象必须携带当前 `ontology_version`，用于未来本体升级后的重对齐

---

## 5.7 流水线质量监控指标

| 阶段 | 关键指标 | 告警阈值 |
|---|---|---|
| 采集 | 抓取成功率 | < 80% 告警 |
| 正文抽取 | 有效文本率（非 low_quality） | < 70% 告警 |
| 切分 | 平均 token_count 合理范围 | < 30 或 > 1024 比例 > 10% 告警 |
| 本体对齐 | canonical tag 命中率 | < 60% 告警（可能本体覆盖不足） |
| 关系抽取 | 违约关系比例 | > 20% 告警 |
| 事实去重 | 重复事实压缩率 | < 5% 可能去重未生效 |
| 冲突检测 | 冲突率 | > 10% 需检查语料质量或本体设计 |
| 入库 | 门控拒绝率 | > 30% 告警（来源质量或抽取质量差） |

---

# 六、MVP 开发任务拆解（Phase 1 & 2 细化）

## Phase 1 核心任务（4~6 周）

| 任务 | 产出 | 优先级 |
|---|---|---|
| 搭建 PostgreSQL + Neo4j + MinIO 基础环境 | 数据库可用 | P0 |
| 执行本文 DDL，建立 PG 表结构 | 元数据库就绪 | P0 |
| 执行 Neo4j Schema 初始化（约束 + 索引） | 图库就绪 | P0 |
| 导入 ontology/top/relations.yaml | 关系类型表就绪 | P0 |
| 导入 ontology/domains/ip_network.yaml（约 60 节点） | IP 子域本体就绪 | P0 |
| 导入 ontology/lexicon/aliases.yaml | 别名词典就绪 | P1 |
| 实现 source_registry 管理和 crawl_tasks 调度基础 | 采集框架就绪 | P1 |
| 实现 IETF RFC 正文抽取 pipeline（rules C1~C5） | 第一条 S 级数据入库 | P1 |
| 实现语义切分 rules S1~S4 | segment 入库 | P1 |
| 实现本体对齐 rules A1~A5 | segment_tags 生成 | P1 |

## Phase 2 核心任务（4~6 周）

| 任务 | 产出 | 优先级 |
|---|---|---|
| 实现关系抽取 rules R1~R4（规则模板为主） | Fact + Evidence 入库 | P0 |
| 实现事实去重 rules D1~D5 | 去重融合就绪 | P0 |
| 实现 semantic_lookup / semantic_resolve API | 基础查询可用 | P0 |
| 实现 semantic_expand API | 图谱导航可用 | P1 |
| 接入 Cisco / Huawei 官方文档采集 | A 级数据入库 | P1 |
| 实现候选概念发现 candidate_discover API | 演化闭环基础 | P1 |
| 基础质量监控仪表盘 | 流水线可观测 | P1 |

---

*文档版本：v0.1 | 生成日期：2026-03-22*

---

# 附录：实施完成情况（v0.2.0，2026-03-31 更新）

## Phase 1 完成清单

| 任务 | 状态 | 实现说明 |
|------|------|----------|
| 搭建 PostgreSQL + Neo4j + MinIO 基础环境 | ✅ 完成 | docker-compose.yml，端口映射到 localhost |
| 执行 PG 表结构 DDL | ✅ 完成 | scripts/init_postgres.sql，13+ 张表含 pgvector extension |
| 执行 Neo4j Schema 初始化 | ✅ 完成 | scripts/init_neo4j.py，约束 + 索引 |
| 导入 relations.yaml | ✅ 完成 | 54 种受控关系类型 |
| 导入 ip_network.yaml | ✅ 完成 | 153 节点（含 v0.2.0 新增 TCP/UDP/IP/HTTP/TLS/SSH/Transport/Application 8 节点） |
| 导入 aliases.yaml | ✅ 完成 | 834 条别名（lexicon 156 + 各 domain 内联别名） |
| source_registry 管理和 crawl_tasks 调度 | ✅ 完成 | worker.py 自动注册 3 个数据源 + 种子 URL |
| IETF RFC 正文抽取 pipeline（C1~C5） | ✅ 完成 | stage1_ingest.py，含纯文本检测 + preserve_newlines |
| 语义切分 S1~S4 | ✅ 完成 | stage2_segment.py，支持 Markdown / RFC / 纯文本三种格式 |
| 本体对齐 A1~A5 | ✅ 完成 | stage3_align.py，词边界匹配 + 候选归一化 upsert |

## Phase 2 完成清单

| 任务 | 状态 | 实现说明 |
|------|------|----------|
| 关系抽取 R1~R4 | ✅ 完成 | stage4_extract.py，15 规则模板 + LLM 双通道 |
| 事实去重 D1~D5 | ✅ 完成 | stage5_dedup.py，SimHash + Jaccard + 冲突检测 |
| semantic_lookup / semantic_resolve API | ✅ 完成 | 15 个算子全部经 OperatorRegistry 分发 |
| semantic_expand API | ✅ 完成 | Neo4j 图邻域遍历 |
| 候选概念发现 candidate_discover | ✅ 完成 | stage3 自动发现 + stage3b 自动评分门控 |
| 接入 Cisco / Huawei 文档采集 | ⏳ 未开始 | 当前仅 RFC/3GPP/ITU 三个种子源 |
| 基础质量监控仪表盘 | ⏳ 未开始 | 有 /health API，无可视化 dashboard |

## 超出原计划的额外实现

| 功能 | 说明 |
|------|------|
| **semcore 框架包** | 完整的 ABC 抽象层（5 Provider + Stage + Operator + Governance），支持独立发布 |
| **Stage 3b：本体自动演化** | 候选归一化 → 五维评分 → 六项门控 → 自动/人工晋升，完整闭环 |
| **多模 LLM 支持** | 兼容 Anthropic / OpenAI / DeepSeek / 通义千问 API |
| **LLM 熔断器** | 连续 3 次失败自动禁用 10 分钟，防止 pipeline 卡死 |
| **反爬虫对抗** | curl_cffi TLS 指纹模拟 + SSL 降级 + 自动降级策略 |
| **内容寻址存储** | MinIO key = SHA-256(content)，重试不覆盖，幂等写入 |
| **Worker 智能调度** | 失败重试（3 次渐进退避）+ 空转指数退避 |
| **本地开发模式** | run_dev.py，SQLite + dict 替代真实数据库，零依赖启动 |
| **数据库分库分 schema** | 爬虫表 → telecom_crawler 独立库；治理表 → governance schema；t_edu_detail 合并入 segments |
| **RST 关系类型扩展** | 11 → 21 种通用 RST 类型，按 6 个逻辑类别组织，规则映射 13 → 37 条 |
| **爬虫与 Pipeline 解耦** | Spider 移到 Pipeline 外部，Stage 1 纯清洗，extractor/normalizer 移到 pipeline/preprocessing |
| **RFC 纯文本分段** | 自动检测 .txt 格式，按编号标题 / 全大写标题 / 分页符切分 |
| **动态段落置信度** | 基于长度 / 语义角色 / 技术术语密度的启发式评分 |

## 文件结构（当前 v0.2.0）

```
Self_Knowledge_Evolve/
├── semcore/semcore/                ← 框架包（可独立发布）
│   ├── core/types.py              13 个领域数据类
│   ├── core/context.py            PipelineContext
│   ├── providers/base.py          5 个 Provider ABC
│   ├── ontology/base.py           OntologyProvider ABC
│   ├── governance/base.py         3 个治理 ABC
│   ├── operators/base.py          SemanticOperator + Registry + Middleware
│   ├── pipeline/base.py           Stage + Pipeline (linear/branch/switch)
│   └── app.py                     SemanticApp + AppConfig
│
├── src/
│   ├── app.py                     FastAPI 入口
│   ├── app_factory.py             组合根 build_app()
│   ├── config/settings.py         Pydantic Settings（.env 驱动）
│   │
│   ├── providers/                 6 个 Provider 实现
│   │   ├── postgres_store.py      RelationalStore → psycopg2（知识库）
│   │   ├── crawler_postgres_store.py  RelationalStore → psycopg2（爬虫库）
│   │   ├── neo4j_store.py         GraphStore → neo4j driver
│   │   ├── anthropic_llm.py       LLMProvider → OpenAI/Anthropic 兼容
│   │   ├── bge_m3_embedding.py    EmbeddingProvider → SentenceTransformer
│   │   └── minio_store.py         ObjectStore → MinIO S3
│   │
│   ├── ontology/
│   │   ├── registry.py            OntologyRegistry（单例，YAML → 内存）
│   │   ├── yaml_provider.py       YAMLOntologyProvider（alias_map/relation_ids/nodes 属性）
│   │   └── validator.py           YAML 校验
│   │
│   ├── governance/
│   │   ├── confidence_scorer.py   五维置信度评分
│   │   ├── conflict_detector.py   冲突检测（same S+P, different O）
│   │   └── evolution_gate.py      六项门控（阈值来自 evolution_policy.yaml）
│   │
│   ├── pipeline/
│   │   ├── pipeline_factory.py    build_pipeline() → 7 阶段
│   │   ├── runner.py              批量 runner（使用 source_doc_id 驱动）
│   │   ├── preprocessing/
│   │   │   ├── extractor.py       HTML 正文提取（trafilatura/readability）
│   │   │   └── normalizer.py      去噪归一化 + hash
│   │   └── stages/
│   │       ├── stage1_ingest.py   文档清洗（C3-C5，纯清洗，不感知数据来源）
│   │       ├── stage2_segment.py  语义切分（S1-S4，RFC/Markdown/纯文本）
│   │       ├── stage3_align.py    本体对齐（A1-A5，词边界匹配）
│   │       ├── stage3b_evolve.py  本体自动学习（评分/门控/晋升）
│   │       ├── stage4_extract.py  关系抽取（R1-R4 + LLM，熔断器）
│   │       ├── stage5_dedup.py    去重融合（D1-D5，SimHash + 冲突检测）
│   │       └── stage6_index.py    图谱索引（I1-I3 + Embedding，Neo4j name 属性）
│   │
│   ├── operators/                 15 个 SemanticOperator（均通过 app.query() 分发）
│   ├── api/semantic/              9 个算子业务逻辑 + router.py
│   ├── crawler/                   Spider（curl_cffi + SSL 降级），Pipeline 外部数据源
│   ├── utils/                     hashing / confidence / embedding / llm_extract / normalize / text / health
│   ├── db/
│   │   ├── postgres.py            知识库连接池
│   │   ├── crawler_postgres.py    爬虫库连接池
│   │   └── neo4j_client.py        Neo4j driver
│   └── dev/                       fake_postgres + fake_crawler_postgres + fake_neo4j + seed
│
├── ontology/
│   ├── domains/                   5 个 YAML（153 节点）
│   ├── lexicon/aliases.yaml       156 条别名
│   ├── top/relations.yaml         54 种关系类型
│   └── governance/evolution_policy.yaml  演化策略（门控阈值 + 权重 + 反漂移）
│
├── scripts/
│   ├── init_postgres.sql          知识库 DDL（public + governance schema）
│   ├── init_crawler_postgres.sql  爬虫库 DDL（source_registry / crawl_tasks / extraction_jobs）
│   ├── init_neo4j.py              约束 + 索引
│   ├── load_ontology.py           YAML → Neo4j + PG
│   └── migrations/
│       ├── 001_evolution_normalize.sql
│       ├── 002_merge_edu_into_segments.sql
│       └── 003_governance_schema.sql
│
├── worker.py                      后台 Worker（爬取 + Pipeline + 重试 + 退避）
├── run_dev.py                     本地开发入口（SQLite + dict，零外部依赖）
├── start.bat                      生产启动（FastAPI + Worker）
└── docker-compose.yml             PostgreSQL + Neo4j + MinIO
```

## 配置参数一览

### .env 必填项

| 参数 | 说明 | 示例 |
|------|------|------|
| POSTGRES_HOST | PG 主机 | 127.0.0.1 |
| POSTGRES_PORT | PG 端口 | 5432 |
| POSTGRES_DB | 数据库名 | telecom_kb |
| POSTGRES_USER | 用户名 | postgres |
| POSTGRES_PASSWORD | 密码 | (your password) |
| POSTGRES_POOL_MIN / MAX | 连接池 | 2 / 10 |
| NEO4J_URI | Neo4j 连接 | bolt://127.0.0.1:7687 |
| NEO4J_USER / PASSWORD | 认证 | neo4j / (your password) |
| NEO4J_DATABASE | 数据库 | neo4j |
| MINIO_ENDPOINT | MinIO 地址 | 127.0.0.1:9001 |
| MINIO_ACCESS_KEY / SECRET_KEY | 认证 | minio / (your password) |

### .env 可选项

| 参数 | 默认值 | 说明 |
|------|--------|------|
| LLM_ENABLED | false | 启用 LLM 抽取 |
| LLM_API_KEY | (空) | API 密钥 |
| LLM_BASE_URL | https://api.anthropic.com | API 地址 |
| LLM_MODEL | claude-haiku-4-5-20251001 | 模型名 |
| EMBEDDING_ENABLED | false | 启用向量嵌入 |
| EMBEDDING_MODEL | BAAI/bge-m3 | 嵌入模型 |
| EMBEDDING_DEVICE | cpu | cpu 或 cuda |
| ONTOLOGY_VERSION | v0.2.0 | 本体版本标记 |
| CRAWLER_POSTGRES_HOST | (同 POSTGRES_HOST) | 爬虫库主机 |
| CRAWLER_POSTGRES_PORT | (同 POSTGRES_PORT) | 爬虫库端口 |
| CRAWLER_POSTGRES_DB | telecom_crawler | 爬虫库名 |
| CRAWLER_POSTGRES_USER | (同 POSTGRES_USER) | 爬虫库用户 |
| CRAWLER_POSTGRES_PASSWORD | (同 POSTGRES_PASSWORD) | 爬虫库密码 |
| STARTUP_HEALTH_REQUIRED | true | 启动时 PG+Neo4j 必须可用 |
| LOG_LEVEL | INFO | 日志级别 |

## 待优化项

| 优先级 | 项目 | 说明 |
|--------|------|------|
| P0 | LLM 批量抽取 | 合并 5-10 个 segment 到一个 prompt，减少 API 调用次数 |
| P0 | 减小 prompt 体积 | 只发送 segment canonical tags 对应的 node_id，不发全部 100 个 |
| P1 | 跳过不必要 LLM 调用 | 已有 section_title 的 segment 不调 LLM 生成标题 |
| P1 | Embedding 集成 | 下载 BAAI/bge-m3，启用 semantic_search / edu_search |
| P1 | 厂商文档采集 | 接入 Cisco / Huawei 文档源 |
| P2 | 候选概念积累 | 需要更多数据源和多轮爬取才能触发自动晋升 |
| P2 | 质量监控仪表盘 | 基于 Grafana 或自建的 pipeline 可观测性 |

*文档版本：v0.3 | 更新日期：2026-04-01*