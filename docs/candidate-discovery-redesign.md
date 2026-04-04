# 候选概念与候选关系发现 — 重设计方案

**日期**：2026-04-04
**状态**：方案已确认，待开发

---

# 一、问题

## 1.1 候选概念发现用正则，漏检严重

当前 Stage 3 用正则 `[A-Z][A-Za-z0-9\-]{2,}|[A-Z]{2,10}` 发现候选术语：
- 只能抓大写缩写（QUIC、OpenFlow），不能抓多词术语（"route reflector"）、小写术语、中文术语
- 会抓到噪声（"The"、"Section"、"January"）
- normalized_text 全小写导致正则完全失效（已修但治标不治本）

## 1.2 只发现候选概念，不发现候选关系

Stage 4 LLM 返回的三元组中，如果 predicate 不在 54 种受控关系里，当前直接丢弃。但这些未知 predicate 可能是有价值的新关系类型（如 replaces、supersedes、coexists_with）。

---

# 二、方案

## 2.1 候选概念发现：LLM 优先

### Stage 3 对齐时的新流程

```
每个 segment:
  1. alias_map 匹配 → canonical tags（不变）
  2. LLM 提取候选术语：
     Prompt: "从以下技术文本中提取不在已知概念列表中的领域专业术语。
              已知概念: [BGP, OSPF, MPLS, ...]
              文本: {segment.raw_text}
              返回 JSON: [{term, reason}]"
     → 过滤已在本体中的 → 写入 governance.evolution_candidates
  3. LLM 不可用 → 回退到正则（现有逻辑，作为兜底）
```

### LLM prompt 设计

```
System: You are a network engineering terminology extractor.
Given a text segment and a list of known ontology concepts,
identify NEW technical terms that are NOT in the known list
but SHOULD be added to a networking knowledge base.

Return ONLY a JSON array. Each element:
{"term": "<exact surface form>", "reason": "<why this is a domain concept>"}

Rules:
- Only return networking/telecom domain-specific terms
- Skip generic English words, document structure words, author names, dates
- Include: protocol names, mechanisms, configuration objects, network functions
- Include multi-word terms (e.g. "route reflector", "forwarding equivalence class")
- Include abbreviations AND their full forms if both appear
- Return [] if no new terms found
```

### 资源控制

- 不是每个 segment 都调 LLM——只在 segment 有 ≥ 2 个 canonical tags 时才调（说明是技术内容密集段落）
- 已知概念列表只发送 segment 中匹配到的节点 + 同域的节点（不发全部 153 个），控制 prompt 长度

## 2.2 候选关系发现

### Stage 4 抽取时的新流程

```
LLM 返回三元组 (subject, predicate, object):
  if predicate in 54 种受控关系:
    → 正常创建 Fact（不变）
  else:
    → 写入 governance.relation_candidates
      记录: predicate_name, subject_example, object_example,
            source_doc_id, segment_id, extraction_count
```

### 新表 governance.relation_candidates

```sql
CREATE TABLE governance.relation_candidates (
    id                  BIGSERIAL PRIMARY KEY,
    candidate_id        UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    predicate_name      VARCHAR(128) NOT NULL,
    normalized_name     VARCHAR(128),
    examples            JSONB        DEFAULT '[]',    -- [{subject, object, segment_id, source_doc_id}]
    source_count        INTEGER      NOT NULL DEFAULT 1,
    source_diversity    NUMERIC(4,3) DEFAULT 0.0,
    review_status       VARCHAR(32)  NOT NULL DEFAULT 'discovered',
    reviewer            VARCHAR(128),
    review_note         TEXT,
    first_seen_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (normalized_name)
);
```

### UPSERT 逻辑

```python
# Stage 4: LLM 返回 predicate 不在受控集中时
store.execute("""
    INSERT INTO governance.relation_candidates
        (predicate_name, normalized_name, examples, source_count, first_seen_at, last_seen_at)
    VALUES (%s, %s, %s::jsonb, 1, NOW(), NOW())
    ON CONFLICT (normalized_name) DO UPDATE SET
        source_count = governance.relation_candidates.source_count + 1,
        last_seen_at = NOW(),
        examples = governance.relation_candidates.examples || %s::jsonb
""", (predicate, normalized, json.dumps([example]), json.dumps([example])))
```

## 2.3 关系候选门控

复用 evolution_candidates 的门控思路，但阈值不同：

| 门控 | 阈值 | 含义 |
|------|------|------|
| source_count | ≥ 5 | 至少 5 次出现 |
| source_diversity | ≥ 0.4 | 来自 2+ 个不同文档 |
| review_status | pending_review → accepted | 人工审核后加入 relations.yaml |

关系候选通过审核后：
1. 加入 `ontology/top/relations.yaml`
2. 重新加载 OntologyRegistry
3. 之前丢弃的三元组可以回溯重建

---

# 三、对 LLM prompt 的修改

### Stage 3 新增 prompt（候选概念）

`src/utils/llm_extract.py` 新增方法：
```python
def extract_candidate_terms(self, text: str, known_terms: list[str]) -> list[dict]:
    """Extract domain terms not in known_terms list."""
```

### Stage 4 修改（候选关系）

`extract_facts_llm` 返回的三元组不再丢弃未知 predicate，而是分流：
- 已知 predicate → facts 表
- 未知 predicate → relation_candidates 表

---

# 四、文件变更

| 文件 | 改动 |
|------|------|
| `scripts/init_postgres.sql` | 新增 `governance.relation_candidates` 表 |
| `scripts/migrations/005_relation_candidates.sql` | 迁移脚本 |
| `src/utils/llm_extract.py` | 新增 `extract_candidate_terms()` 方法 |
| `src/pipeline/stages/stage3_align.py` | `_collect_candidates` 改为 LLM 优先 + 正则兜底 |
| `src/pipeline/stages/stage4_extract.py` | LLM 返回未知 predicate → 写 relation_candidates |
| `src/dev/fake_postgres.py` | 新增 relation_candidates 表 |
| `src/stats/collector.py` | evolution 指标增加 relation_candidates 统计 |
| `src/stats/drilldown.py` | 新增 relation_candidates 下钻 |

---

# 五、监控看板新增指标

| 指标 | 下钻 |
|------|------|
| relation_candidates_total | → candidate_discover 扩展 |
| relation_candidates_by_status | → drilldown |
| top_unknown_predicates | → 按出现频次排序的未知 predicate 列表 |
