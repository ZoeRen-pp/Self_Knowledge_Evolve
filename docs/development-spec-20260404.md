# 语义知识操作系统 — 开发方案文档

**版本**：v0.4
**日期**：2026-04-04

---

# 一、数据库表结构

## 1.1 知识库 telecom_kb — public schema

### documents
```sql
CREATE TABLE documents (
    id                  BIGSERIAL PRIMARY KEY,
    source_doc_id       UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    crawl_task_id       BIGINT,
    site_key            VARCHAR(64) NOT NULL,
    source_url          TEXT NOT NULL,
    canonical_url       TEXT,
    title               TEXT,
    doc_type            VARCHAR(32),    -- spec|vendor_doc|pdf|faq|tutorial|tech_article
    language            CHAR(5) DEFAULT 'en',
    source_rank         CHAR(1) NOT NULL,  -- S|A|B|C
    publish_time        TIMESTAMPTZ,
    crawl_time          TIMESTAMPTZ NOT NULL,
    content_hash        CHAR(64),
    normalized_hash     CHAR(64),
    raw_storage_uri     TEXT,
    cleaned_storage_uri TEXT,
    status              VARCHAR(32) DEFAULT 'raw',
                        -- raw → cleaned → segmented → indexed | deduped | low_quality | failed
    dedup_group_id      UUID,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
```

### segments
```sql
CREATE TABLE segments (
    id                  BIGSERIAL PRIMARY KEY,
    segment_id          UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    source_doc_id       UUID NOT NULL REFERENCES documents(source_doc_id),
    section_path        TEXT[],
    section_title       TEXT,
    segment_index       INTEGER NOT NULL,
    segment_type        VARCHAR(32) NOT NULL,
    raw_text            TEXT NOT NULL,
    normalized_text     TEXT,
    token_count         INTEGER,
    confidence          NUMERIC(4,3) DEFAULT 1.0,
    simhash_value       BIGINT,
    embedding           vector(1024),
    title               VARCHAR(255),
    title_vec           vector(1024),
    content_vec         vector(1024),
    content_source      VARCHAR(128),
    lifecycle_state     VARCHAR(32) DEFAULT 'active',
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);
```

### t_rst_relation
```sql
CREATE TABLE t_rst_relation (
    nn_relation_id  VARCHAR(36) PRIMARY KEY,
    relation_type   VARCHAR(255) NOT NULL,  -- 21 种 RST 类型
    src_edu_id      UUID REFERENCES segments(segment_id),
    dst_edu_id      UUID REFERENCES segments(segment_id),
    meta_context    JSONB,
    relation_source VARCHAR(255),  -- llm | rule
    update_time     TIMESTAMPTZ DEFAULT NOW(),
    reliability     BIGINT DEFAULT 1
);
```

### segment_tags / facts / evidence / lexicon_aliases / system_stats_snapshots

（DDL 见 `scripts/init_postgres.sql`）

## 1.2 知识库 telecom_kb — governance schema

### governance.evolution_candidates
```sql
CREATE TABLE governance.evolution_candidates (
    id                       BIGSERIAL PRIMARY KEY,
    candidate_id             UUID NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_forms            TEXT[] NOT NULL,
    normalized_form          VARCHAR(256),
    candidate_parent_id      VARCHAR(128),
    source_count             INTEGER DEFAULT 0,
    source_diversity_score   NUMERIC(4,3) DEFAULT 0.0,
    temporal_stability_score NUMERIC(4,3) DEFAULT 0.0,
    structural_fit_score     NUMERIC(4,3) DEFAULT 0.0,
    synonym_risk_score       NUMERIC(4,3) DEFAULT 0.0,
    composite_score          NUMERIC(4,3) DEFAULT 0.0,
    review_status            VARCHAR(32) DEFAULT 'discovered',
    candidate_type           VARCHAR(32) DEFAULT 'concept',  -- concept | relation
    examples                 JSONB DEFAULT '[]',
    first_seen_at            TIMESTAMPTZ DEFAULT NOW(),
    last_seen_at             TIMESTAMPTZ DEFAULT NOW(),
    accepted_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ DEFAULT NOW()
);
```

### governance.conflict_records / review_records / ontology_versions

（DDL 见 `scripts/init_postgres.sql`）

## 1.3 爬虫库 telecom_crawler

source_registry / crawl_tasks / extraction_jobs（DDL 见 `scripts/init_crawler_postgres.sql`）

## 1.4 Neo4j Schema

**节点标签**（多标签，按 ID 前缀自动打标）：

| 前缀 | 基础标签 | 附加标签 | 数量 |
|------|----------|----------|------|
| IP.  | OntologyNode | — | 74 |
| MECH. | OntologyNode | MechanismNode | 24 |
| METHOD. | OntologyNode | MethodNode | 22 |
| COND. | OntologyNode | ConditionRuleNode | 20 |
| SCENE. | OntologyNode | ScenarioPatternNode | 13 |

**知识边**：动态类型，MERGE by (src, type, dst)，不产生重复边。
```cypher
MERGE (a)-[r:{REL_TYPE}]->(b)
SET r.predicate = $predicate,
    r.confidence = CASE WHEN r.confidence < $conf THEN $conf ELSE r.confidence END,
    r.fact_count = coalesce(r.fact_count, 0) + 1
```

---

# 二、本体 YAML 结构

```
ontology/
├── top/relations.yaml              # 54 种受控关系类型
├── domains/                        # 5 个领域 YAML, 153 节点
│   ├── ip_network.yaml             # 74 concept
│   ├── ip_network_mechanisms.yaml  # 24 mechanism
│   ├── ip_network_methods.yaml     # 22 method
│   ├── ip_network_conditions.yaml  # 20 condition
│   └── ip_network_scenarios.yaml   # 13 scenario
├── lexicon/aliases.yaml            # 156 别名
├── seeds/                          # 种子关系 + 分类修正
│   ├── cross_layer_relations.yaml  # 56 跨层关系 (explains/implemented_by/applies_to/composed_of)
│   ├── axiom_relations.yaml        # 48 公理关系 (is_a/uses_protocol/depends_on/encapsulates/part_of)
│   └── classification_fixes.yaml   # 3 分类纠正 (BFD→OAM, DHCP→Transport, DNS→AppLayer)
├── patterns/                       # 外部化正则（代码不含领域知识）
│   ├── semantic_roles.yaml         # 22 patterns → 12 语义角色 (Stage 2)
│   ├── context_signals.yaml        # 6 patterns → 6 上下文标签 (Stage 3)
│   └── predicate_signals.yaml      # 13 patterns → 13 谓语信号 (Stage 4 co-occurrence)
└── governance/evolution_policy.yaml # 门控阈值 + 权重
```

---

# 三、7 阶段流水线

## Stage 1: 清洗 (Ingest/Clean)

**输入**：source_doc_id (documents 表 status='raw')

**处理**：
1. 从 MinIO 加载 raw_storage_uri
2. ContentExtractor.extract() — trafilatura → readability → 正则去标签（三级降级）
3. DocumentNormalizer.normalize() — 去噪 + Unicode 归一化
4. content_hash 去重（重复 → status='deduped'）
5. 质量门控（token < 200 → status='low_quality'）
6. doc_type 检测
7. 存 cleaned 到 MinIO

**输出**：documents.status → 'cleaned'

## Stage 2: 切段 + RST (Segment)

**输入**：cleaned text

**切分策略**（三级）：
1. 结构切分：Markdown 标题 / RFC 编号节标题 / 纯文本段落
2. 超长段落处理：
   - Level 1: 段落边界（`\n\n`）
   - Level 2: 句号边界（贪心合并到 ~512 tokens）
   - Level 3: 滑窗兜底（window=512, overlap=64）
3. 语义角色分类：从 `ontology/patterns/semantic_roles.yaml` 加载正则
4. RST 关系：LLM 判定 21 种类型 → 规则回退 → 逐条标记来源

**输出**：segments + t_rst_relation + documents.status → 'segmented'

## Stage 3: 对齐 (Align)

**输入**：segments

**处理**：
- alias_map 匹配 → canonical + 五层标签
- 候选概念发现：**LLM 优先**（`extract_candidate_terms`）→ regex 兜底
- 上下文标签：从 `ontology/patterns/context_signals.yaml` 加载正则

**输出**：segment_tags + governance.evolution_candidates (type='concept')

## Stage 3b: 演化 (Evolve)

五维评分 → 六项门控 → auto_accept (≥0.85 + ≥7天) / pending_review

## Stage 4: 抽取 (Extract)

**优先级链**（无正则）：
1. **LLM 抽取**（最高质量）→ 有结果则完成
2. **Merged context retry** → LLM 返空时，合并前一个 segment（如 RST 关系为连续性），重试 LLM
3. **Co-occurrence 兜底** → 恰好 2 个节点 + 1 个谓语信号 → 最多 1 条 fact

未知 predicate → evolution_candidates (type='relation')

**输出**：facts + evidence (extraction_method: llm/cooccurrence)

## Stage 5: 去重 (Dedup)

SimHash + Jaccard 段落去重 → (S,P,O) 精确合并 → 冲突检测

## Stage 6: 索引 (Index)

置信度门控 → Neo4j 动态边类型（MERGE 无重复）→ 向量嵌入

---

# 四、20 个语义算子 API

Base URL: `/api/v1/semantic`

| 方法 | 端点 | 算子 | 说明 |
|------|------|------|------|
| GET | /lookup | lookup | 术语 → 本体节点 + 证据 |
| GET | /resolve | resolve | 别名 → 标准节点 |
| GET | /expand | expand | 节点邻域 (depth 1-3) |
| POST | /filter | filter | 参数化过滤 + 分页 |
| GET | /path | path | 两节点最短路径 |
| GET | /dependency_closure | dependency_closure | 依赖闭包 BFS |
| POST | /impact_propagate | impact_propagate | 故障影响链路 |
| GET | /evidence_rank | evidence_rank | 事实证据排序 |
| GET | /conflict_detect | conflict_detect | 矛盾检测 |
| POST | /fact_merge | fact_merge | 事实合并 |
| GET | /candidate_discover | candidate_discover | 候选概念发现 |
| GET | /attach_score | attach_score | 候选评分 |
| POST | /evolution_gate | evolution_gate | 门控评审 |
| POST | /semantic_search | semantic_search | 段落向量搜索 |
| POST | /edu_search | edu_search | 双向量加权搜索 |
| GET | /graph_inspect | graph_inspect | 图结构检查 (5 种 inspect_type) |
| GET | /cross_layer_check | cross_layer_check | 五层覆盖率 |
| GET | /ontology_inspect | ontology_inspect | 本体工程检查 (5 种 inspect_type) |
| GET | /stale_knowledge | stale_knowledge | 知识时效查询 |
| GET | /ontology_quality | ontology_quality | 全量质量报告 (5 维 20 指标) |

## 系统 API

| 方法 | 端点 | 说明 |
|------|------|------|
| GET | /api/v1/system/stats | 最新指标快照 |
| GET | /api/v1/system/stats/history | 趋势数据 |
| GET | /api/v1/system/drilldown/{metric} | 异常下钻 (21 指标) |
| GET | /api/v1/system/review | 列出候选 |
| GET | /api/v1/system/review/{id} | 候选详情 |
| POST | /api/v1/system/review/{id}/approve | 审批通过 |
| POST | /api/v1/system/review/{id}/reject | 拒绝 |

---

# 五、数据源接入

任何数据源只需：
1. 往 documents 表 INSERT 一条 `status='raw'`
2. 往 MinIO raw/ 存原始文档

Pipeline worker 自动捞取处理。

---

# 六、配置参数

| 参数 | 默认 | 说明 |
|------|------|------|
| POSTGRES_HOST/PORT/DB/USER/PASSWORD | — | 知识库（必填） |
| CRAWLER_POSTGRES_* | 同主库 | 爬虫库（留空复用） |
| NEO4J_URI/USER/PASSWORD | bolt://localhost:7687 | Neo4j |
| MINIO_ENDPOINT/ACCESS_KEY/SECRET_KEY | — | MinIO |
| LLM_ENABLED | false | 启用 LLM |
| LLM_API_KEY / LLM_BASE_URL / LLM_MODEL | — | LLM 配置 |
| EMBEDDING_ENABLED | false | 启用向量嵌入 |
| ONTOLOGY_VERSION | v0.2.0 | 本体版本 |

---

# 七、本地开发

```bash
python run_dev.py
```

注入 fake_postgres + fake_crawler_postgres + fake_neo4j（SQLite + dict），从 YAML 自动 seed。

---

# 八、项目文件结构

```
Self_Knowledge_Evolve/
├── semcore/semcore/                    # 框架包（零依赖 ABCs）
├── src/
│   ├── app.py                          # FastAPI 入口
│   ├── app_factory.py                  # build_app() 组合根
│   ├── config/settings.py
│   ├── db/                             # postgres + crawler_postgres + neo4j_client
│   ├── providers/                      # 6 个 Provider 实现
│   ├── ontology/                       # OntologyRegistry + YAMLProvider + validator
│   ├── governance/                     # 置信度 + 冲突 + 演化门控
│   ├── pipeline/
│   │   ├── preprocessing/              # extractor + normalizer
│   │   ├── pipeline_factory.py
│   │   └── stages/                     # 7 个 Stage
│   ├── operators/                      # 20 个 SemanticOperator
│   ├── api/
│   │   ├── semantic/                   # 算子业务逻辑 + router
│   │   └── system/                     # 监控 + 审批 router + review.py
│   ├── stats/                          # collector + scheduler + drilldown + backfill + ontology_quality
│   ├── crawler/spider.py               # HTTP 爬虫（Pipeline 外部）
│   ├── utils/                          # text/hashing/confidence/embedding/llm_extract/logging
│   └── dev/                            # fake stores + seed
├── ontology/                           # YAML 唯一源头
│   ├── domains/ · lexicon/ · top/ · seeds/ · patterns/ · governance/
├── scripts/                            # DDL + migrations + load_ontology
├── static/dashboard.html               # 前端看板
├── worker.py                           # 后台 Worker
└── run_dev.py                          # 开发入口
```
