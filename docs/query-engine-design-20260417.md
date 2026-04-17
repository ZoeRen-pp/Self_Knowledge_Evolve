# 查询引擎设计 — 基于图关系代数的声明式查询/召回层
**日期：2026-04-17 | 版本：0.1（草案）**

---

## 1. 为什么需要重构查询层

### 1.1 现状的结构性问题

现有 21 个语义算子是 21 个独立的过程式函数，各自硬编码了遍历逻辑：

- `dependency_closure` — 60 行 BFS，只沿 DEPENDS_ON/REQUIRES 走
- `impact_propagate` — 另一套 BFS，沿 CAUSES/IMPACTS 走，带置信度衰减
- `context_assemble` — 硬编码的 4 步 Cypher 链（EXPLAINS→IMPLEMENTED_BY→APPLIES_TO→COMPOSED_OF）
- `expand` — 单跳邻居查询

**问题一：不可组合。** 工程师问"改 BGP timer 会影响哪些场景的哪些配置步骤？"需要 impact_propagate + 跨层穿越 + context_assemble 串联。现在必须由应用层手动编排三次 API 调用，结果手动拼接。

**问题二：不正交。** 21 个算子里有大量重叠——dependency_closure 和 impact_propagate 本质上是同一个 BFS 的不同参数化，cross_layer_check 是 expand 的特化。加新推理模式必须写新代码。

**问题三：不可被 LLM agent 自主使用。** 算子是固定 API，agent 只能按人类预设的接口调用，无法根据问题动态组合查询计划。

### 1.2 目标

设计一个声明式查询引擎，使得：

1. 查询以 YAML/JSON 声明式描述，LLM 可以动态生成
2. 原语从数据结构的代数性质推导，理论上正交完备
3. 现有 21 个算子可以逐步表达为原语组合，不一次性替换
4. 召回结果支持多信号特征排序 + cross-encoder LLM 精排

---

## 2. 理论基础

### 2.1 数据模型：异构信息网络（HIN）

系统的存储层是一个 HIN，包含多种节点类型和边类型：

**节点类型：**

| 类型 | 存储位置 | 数量级 |
|------|---------|--------|
| OntologyNode（含 5 层子类型） | Neo4j | ~200 |
| Segment | PostgreSQL | ~10K/文档 |
| Document | PostgreSQL | ~1K |
| Fact | Neo4j + PG | ~1K/文档 |
| Evidence | PG | ~1K/文档 |
| Alias | Neo4j | ~800 |

**边类型：**

| 边类型 | 连接 | 数量 |
|--------|------|------|
| 77 种本体关系 | OntologyNode ↔ OntologyNode | 种子 187 + 抽取增量 |
| tagged_in (segment_tags) | OntologyNode → Segment | ~2K/文档 |
| rst_adjacent (t_rst_relation) | Segment → Segment | ~500/文档 |
| supported_by | Fact → Evidence | 1:N |
| extracted_from | Evidence → Segment | N:1 |
| belongs_to | Segment → Document | N:1 |

在 HIN 视角下，"召回段落"不是一个特殊操作，而是从 OntologyNode 沿 tagged_in 边遍历到 Segment 节点——和沿 depends_on 边遍历到其他 OntologyNode 在代数上完全等价。

### 2.2 代数完备性

查询引擎的表达力来自两个代数体系的结合：

**关系代数（Codd 1970）** 的 5 个基本操作对一阶查询完备：

| 操作 | 符号 | 语义 |
|------|------|------|
| 选择 | σ | 按谓词过滤 |
| 投影 | π | 提取特定属性 |
| 笛卡尔积/连接 | ⋈ | 组合两个关系 |
| 并 | ∪ | 合并 |
| 差 | − | 差集（提供否定能力） |

**图查询扩展**需要在关系代数之上添加：

- **传递闭包 TC**：递归可达性查询——这是关系代数无法表达的，也是图区别于关系表的本质能力。对应 Datalog 中的递归规则。

**聚合扩展** γ：计数、求和、排序、分组。不在核心代数中，但实际系统必需（SQL 也在关系代数之外加了 GROUP BY）。

### 2.3 为什么是 5 个原语

将上述代数操作映射到 HIN 上的**节点集变换**，合并可合并的，得到最小正交集：

```
                    ┌───────────────────────────────────────────┐
                    │            查询 = 原语序列               │
                    │                                           │
                    │   seed ──→ expand ──→ combine ──→ ...    │
                    │    σ        ⋈+TC      ∪∩−                │
                    │                                           │
                    │   ... ──→ aggregate ──→ project          │
                    │             γ            π                │
                    └───────────────────────────────────────────┘
```

| # | 原语 | 代数基础 | 输入→输出 | 不可归约原因 |
|---|------|---------|-----------|-------------|
| 1 | **seed** | σ (选择) | Predicate → NodeSet | 唯一的"从无到有"操作——没有输入集合，从全量节点中按谓词选取 |
| 2 | **expand** | ⋈ + TC (连接+闭包) | NodeSet × EdgeSpec → NodeSet | 唯一利用图拓扑结构的操作——沿类型化边遍历，支持递归 |
| 3 | **combine** | ∪ ∩ − (集合代数) | NodeSet × NodeSet → NodeSet | 布尔完备性必需——交集不可由并和差以外的原语导出（虽然 A∩B = A−(A−B)，但 − 本身不可由 ∪ 导出） |
| 4 | **aggregate** | γ (聚合) | NodeSet × Function → Summary/RankedSet | 多→少的归约——从集合中提取统计量或排序，不可由集合操作导出 |
| 5 | **project** | π (投影) | NodeSet × Fields → ProjectedSet | 结构变换——改变每个元素的属性维度，与过滤（改变元素数量）正交 |

**正交性矩阵**（每对原语不可互相表达）：

|  | seed | expand | combine | aggregate | project |
|--|------|--------|---------|-----------|---------|
| seed | — | 不涉及边 | 不涉及两个集合 | 不做归约 | 不改维度 |
| expand | 需要输入集合 | — | 不涉及两个集合 | 不做归约 | 不改维度 |
| combine | 需要两个集合 | 不涉及边 | — | 不做归约 | 不改维度 |
| aggregate | 不选择元素 | 不涉及边 | 不涉及两个集合 | — | 不改维度 |
| project | 不选择元素 | 不涉及边 | 不涉及两个集合 | 不做归约 | — |

**完备性论证：**

- σ + π + ∪ + − 是关系完备的（Codd 定理）
- 加上 TC（通过 expand 的 depth > 1）覆盖了 Datalog-without-negation 的全部表达力
- − （combine/subtract）提供了否定能力（"不在集合中的节点"）
- γ 覆盖了排序/评分/重排，这是实际系统必需但核心代数不含的

任何在这个 HIN 上可以表达的一阶查询+递归可达性+聚合查询，都可以分解为这 5 个原语的组合序列。

---

## 3. 原语详细定义

### 3.1 seed — 选择

从全局节点空间中按谓词创建初始集合。这是所有查询计划的起点。

**参数 schema：**

```yaml
- op: seed
  by: id | alias | layer | embedding | attribute
  value: <varies by 'by'>
  target: node | segment | fact          # 节点类型
  top_k: <int>                           # 仅 embedding 模式
  threshold: <float>                     # 仅 embedding 模式
  as: $variable_name                     # 输出变量名
```

**by 模式：**

| by 值 | value 类型 | 语义 | 执行方式 |
|-------|-----------|------|---------|
| `id` | string[] | 精确 ID 匹配 | PG/Neo4j 主键查找 |
| `alias` | string[] | 别名解析（含大小写归一化） | lexicon_aliases 表查找 |
| `layer` | string | 按本体层选取所有节点 | Neo4j 标签过滤 |
| `embedding` | string | 向量语义相似 | pgvector ANN 搜索 |
| `attribute` | dict | 按属性过滤 | WHERE 条件 |

**示例：**
```yaml
# 按别名锚定
- op: seed
  by: alias
  value: ["BGP", "OSPF"]
  target: node
  as: $protocols

# 按 embedding 语义召回段落
- op: seed
  by: embedding
  value: "BGP timer 变更对邻居关系的影响"
  target: segment
  top_k: 100
  as: $semantic_segments
```

### 3.2 expand — 遍历

沿类型化边遍历图结构，是引擎表达力超越关系代数的核心原语。

**参数 schema：**

```yaml
- op: expand
  from: $variable_name                  # 输入集合
  any_of: [rel_type_1, rel_type_2, ...]  # OR 语义：沿任一类型的边
  sequence: [rel_type_1, rel_type_2]   # 序列语义：先走 type_1 再走 type_2
  direction: outbound | inbound | both
  depth: <int | "unlimited">             # 1=邻居, >1=递归闭包, "unlimited"=不动点, 默认 1
  confidence_decay: <float>             # 每跳置信度衰减系数，默认 1.0（不衰减）
  min_confidence: <float>               # 低于此阈值剪枝
  track_path: <bool>                    # 是否记录完整路径（用于路径查询）
  target: node | segment | fact         # 目标节点类型（HIN 跨类型遍历）
  as: $variable_name
```

**any_of vs sequence 的区别：**

- `any_of: [depends_on, requires]` — 在每一跳中沿 depends_on **或** requires 类型的边走（OR，集合语义）
- `sequence: [explains, implemented_by]` — 第一跳必须走 explains，第二跳必须走 implemented_by（序列语义，跨层穿越）

两者互斥，不能同时指定。

**深度控制的代数含义：**

- `depth: 1` — 单跳连接 ⋈（等价于关系代数的自然连接）
- `depth: k` — k 次迭代连接 ⋈^k
- `depth: "unlimited"` — 传递闭包 TC（等价于 Datalog 递归规则的不动点，受运行时安全边界保护）

**置信度传播模型：**

expand 过程中，每到达一个新节点，置信度按策略更新：

```
到达节点 v 的路径置信度 = 路径上所有边置信度的乘积 × (confidence_decay ^ 跳数)
```

当 `min_confidence` 指定时，低于阈值的分支被剪枝（类似 Datalog 中的 stratified negation 用于避免无穷递归）。

**示例：**
```yaml
# 依赖闭包（=现有 dependency_closure 算子）
- op: expand
  from: $bgp
  any_of: [depends_on, requires]
  direction: outbound
  depth: 6
  as: $all_deps

# 跨层穿越（=现有 context_assemble 的硬编码链）
- op: expand
  from: $bgp
  sequence: [explains, implemented_by, applies_to]
  as: $methods_and_conditions

# 召回段落（HIN 跨类型：OntologyNode → Segment）
- op: expand
  from: $all_deps
  any_of: [tagged_in]
  target: segment
  as: $related_segments

# 影响传播（=现有 impact_propagate 算子）
- op: expand
  from: $fault_node
  any_of: [causes, impacts, depends_on]
  direction: inbound
  depth: 4
  confidence_decay: 0.8
  min_confidence: 0.5
  as: $blast_radius
```

### 3.3 combine — 集合运算

对两个或多个节点集合执行布尔运算。

**参数 schema：**

```yaml
- op: combine
  method: union | intersect | subtract
  sets: [$set_a, $set_b, ...]           # 两个或多个集合
  as: $variable_name
```

**多集合语义：**

- `union: [A, B, C]` → A ∪ B ∪ C（结合律，顺序无关）
- `intersect: [A, B, C]` → A ∩ B ∩ C（结合律，顺序无关）
- `subtract: [A, B]` → A − B（仅支持两个集合，不满足结合律）

**示例：**
```yaml
# 合并两路召回
- op: combine
  method: union
  sets: [$tag_segments, $embed_segments]
  as: $all_candidates

# 找出受影响但不在已有配置中的节点
- op: combine
  method: subtract
  sets: [$blast_radius, $already_configured]
  as: $new_risks
```

### 3.4 aggregate — 聚合/排序/重排

对集合执行归约操作：统计、评分、排序、重排。

**参数 schema：**

```yaml
- op: aggregate
  from: $variable_name
  function: count | rank | group | score | rerank
  by: [field_1, field_2, ...]           # rank/group 的字段
  signals: [signal_1, signal_2, ...]    # score/rerank 的评分信号
  cross_encoder: <bool>                 # rerank 时是否使用 LLM 精排
  query: <string | $variable>           # cross-encoder 的 query 文本
  order: asc | desc
  budget: <int>                         # token 预算（rerank 模式）
  limit: <int>                          # 截断
  as: $variable_name
```

**function 模式：**

| function | 语义 | 输出 |
|----------|------|------|
| `count` | 计数 | 标量 |
| `rank` | 按 by 字段排序 | 有序集合 |
| `group` | 按 by 字段分组 | 分组映射 |
| `score` | 按 signals 加权评分 | 带分数的集合 |
| `rerank` | 多阶段重排序（特征→cross-encoder→预算截断） | 有序集合 |

**rerank 的执行子阶段：**

```
输入候选集（~200 段）
    │
    ▼
Phase 4a: 特征打分（无 LLM，快速）
    - source_rank:      来源权威度（S/A/B/C → 1.0/0.85/0.65/0.40）
    - confidence:       段落置信度
    - anchor_coverage:  命中 expanded_node_set 中多少节点
    - rst_coherence:    是否与其他已选段有 RST 关系
    - freshness:        文档时效性
    │
    ▼ 取 top-N（N 由 cross_encoder_budget 控制，默认 50）
Phase 4b: Cross-encoder 精排（LLM）
    - 将 query 文本 + 候选段落文本送入 LLM
    - LLM 返回 relevance score
    - 与 4a 特征分数加权融合
    │
    ▼
Phase 4c: 预算感知选择
    - 按 token budget 贪心选择
    - RST 连续的段落尽量一起保留（不破坏上下文连贯性）
    - MMR（最大边际相关性）兼顾多样性
    │
    ▼
输出最终排序集合（~30 段，≤ budget tokens）
```

**示例：**
```yaml
# 特征排序（无 LLM）
- op: aggregate
  from: $candidates
  function: rank
  by: [confidence, source_rank]
  order: desc
  limit: 50
  as: $top50

# 完整重排（含 cross-encoder）
- op: aggregate
  from: $all_candidates
  function: rerank
  signals: [source_rank, confidence, anchor_coverage, rst_coherence, freshness]
  cross_encoder: true
  query: "BGP timer 变更对邻居关系的影响"
  budget: 8000
  limit: 30
  as: $final
```

### 3.5 project — 投影

从结果集中提取特定属性，控制输出结构。

**参数 schema：**

```yaml
- op: project
  from: $variable_name
  fields: [field_1, field_2, ...]
  as: $variable_name
```

**示例：**
```yaml
- op: project
  from: $final
  fields: [segment_id, text, confidence, source_url, matched_nodes, rerank_score]
```

---

## 4. 查询计划的完整示例

### 4.1 变更影响面评估

工程师问："如果关闭 OSPF Area 0 接口，影响面是什么？给我相关的配置方法和证据。"

```yaml
name: ospf_area0_shutdown_impact
intent: "关闭 OSPF Area 0 接口的影响面评估"

steps:
  # 1. 锚定节点
  - op: seed
    by: alias
    value: ["OSPF", "Area 0"]
    target: node
    as: $ospf

  # 2. 正向影响传播
  - op: expand
    from: $ospf
    any_of: [causes, impacts, depends_on]
    direction: inbound
    depth: 4
    confidence_decay: 0.8
    min_confidence: 0.5
    as: $blast

  # 3. 跨层穿越到配置方法
  - op: expand
    from: $blast
    sequence: [explains, implemented_by]
    as: $methods

  # 4. 合并影响节点和相关方法
  - op: combine
    method: union
    sets: [$blast, $methods]
    as: $all_relevant

  # 5. 召回相关段落（tag 路）
  - op: expand
    from: $all_relevant
    any_of: [tagged_in]
    target: segment
    as: $tag_segments

  # 6. 召回补充段落（embedding 路）
  - op: seed
    by: embedding
    value: "OSPF Area 0 接口关闭影响"
    target: segment
    top_k: 50
    as: $embed_segments

  # 7. 合并两路召回
  - op: combine
    method: union
    sets: [$tag_segments, $embed_segments]
    as: $candidates

  # 8. 重排（特征 + cross-encoder）
  - op: aggregate
    from: $candidates
    function: rerank
    signals: [source_rank, confidence, anchor_coverage, rst_coherence, freshness]
    cross_encoder: true
    query: "OSPF Area 0 接口关闭影响"
    budget: 8000
    limit: 30
    as: $final

  # 9. 输出
  - op: project
    from: $final
    fields: [segment_id, text, confidence, source_url, matched_nodes, rerank_score]
```

### 4.2 现有算子的原语映射

每个现有算子都可以表达为原语组合，证明系统向后兼容：

| 现有算子 | 原语组合 |
|---------|---------|
| `lookup` | seed(by=id) |
| `resolve` | seed(by=alias) |
| `expand` | seed → expand(depth=1) |
| `path` | seed(两端点) → expand(track_path=true) |
| `dependency_closure` | seed → expand(any_of=[depends_on,requires], depth=6) |
| `impact_propagate` | seed → expand(any_of=[causes,impacts], depth=4, confidence_decay=0.8) |
| `filter` | seed(by=attribute) 或 combine(intersect, seed(...)) |
| `context_assemble` | seed → expand(sequence=[explains,implemented_by,applies_to]) → expand(any_of=[tagged_in]) → aggregate(rerank) → project |
| `semantic_search` | seed(by=embedding) |
| `conflict_detect` | seed → expand → combine(intersect) → aggregate(group) |
| `evidence_rank` | seed → expand(any_of=[supported_by,extracted_from]) → aggregate(rank) |
| `fact_merge` | seed → expand → combine → aggregate |
| `cross_layer_check` | seed(by=layer) → expand(sequence=...) → aggregate(count) |

---

## 5. 引擎架构

### 5.1 分层结构

```
┌──────────────────────────────────────────────────────────────┐
│  API 层       POST /api/v1/query                             │
│               接收 YAML/JSON 查询计划                        │
├──────────────────────────────────────────────────────────────┤
│  校验层       QueryValidator                                 │
│               schema 校验 + 变量引用检查 + 边类型合法性      │
├──────────────────────────────────────────────────────────────┤
│  规划层       QueryPlanner                                   │
│               变量依赖分析 → 执行顺序 → 并行机会检测         │
├──────────────────────────────────────────────────────────────┤
│  执行层       QueryExecutor                                  │
│               5 个原语执行器，操作 WorkingMemory              │
│               SeedExecutor / ExpandExecutor / CombineExecutor │
│               AggregateExecutor / ProjectExecutor            │
├──────────────────────────────────────────────────────────────┤
│  存储适配层   现有 GraphStore / RelationalStore / Embedding   │
│               Neo4j (graph) / PG (segments,facts) / pgvector │
└──────────────────────────────────────────────────────────────┘
```

### 5.2 WorkingMemory

查询执行过程中的中间状态容器。每个 `$variable` 对应 WorkingMemory 中的一个槽位。

```
WorkingMemory = dict[str, ResultSet]

ResultSet:
    nodes: list[NodeRef]          # 节点 ID + 类型 + 属性快照
    provenance: list[StepTrace]   # 每个节点怎么来的（哪个 step 产生的）
    metadata: dict                # 统计信息
```

所有原语执行器读写 WorkingMemory：seed 写入新槽位，expand 读一个槽位写入新槽位，combine 读两个写一个，以此类推。

### 5.3 QueryValidator

在执行前做静态检查，拦截 LLM 生成的非法查询：

| 检查项 | 规则 |
|--------|------|
| op 合法性 | 必须是 5 个枚举值之一 |
| 变量声明 | 每个 `$var` 必须先 `as:` 声明，再被 `from:` / `sets:` 引用 |
| 边类型合法性 | any_of/sequence 中的值必须在 relations.yaml 或保留边类型集合中 |
| any_of/sequence 互斥 | 同一个 expand 步骤不能同时指定两者 |
| 参数类型 | depth 是正整数、confidence_decay 在 (0,1]、method 是枚举值 |
| 循环依赖 | 变量引用图必须是 DAG |
| 资源上限 | depth ≤ 10、top_k ≤ 500、steps ≤ 20 |

### 5.4 QueryPlanner

分析变量依赖关系，生成执行计划：

```
输入 YAML steps:
  $a = seed(...)
  $b = seed(...)                    # 与 $a 无依赖 → 可并行
  $c = expand(from=$a)              # 依赖 $a
  $d = expand(from=$b)              # 依赖 $b → 与 $c 可并行
  $e = combine(sets=[$c, $d])       # 依赖 $c 和 $d
  $f = aggregate(from=$e)           # 依赖 $e

执行计划（层次化）：
  Wave 0: [$a, $b]          ← 并行
  Wave 1: [$c, $d]          ← 并行
  Wave 2: [$e]
  Wave 3: [$f]
```

### 5.5 ExpandExecutor — 遍历执行器

这是引擎中最复杂的执行器，需要处理：

**any_of 模式（OR 遍历）：**

```
depth=1 → 单次 Cypher MATCH (a)-[:REL1|REL2]->(b)
depth=k → 迭代 BFS，k 轮
depth=∞ → BFS 直到不动点（visited 不再增长）
```

**sequence 模式（序列遍历）：**

```
sequence: [explains, implemented_by, applies_to]
→ MATCH (a)-[:EXPLAINS]->(b)-[:IMPLEMENTED_BY]->(c)-[:APPLIES_TO]->(d)
   用单条 Cypher 多跳查询，或分步执行
```

**跨类型遍历（HIN 特性）：**

当 `target` 指定了不同节点类型时（如 OntologyNode → Segment），执行器切换到 PG 查询：

```
any_of: [tagged_in], target: segment
→ SELECT s.* FROM segments s
   JOIN segment_tags st ON s.segment_id = st.segment_id
   WHERE st.ontology_node_id IN ($node_ids)
```

这里 `tagged_in` 和 `rst_adjacent` 是两个保留边类型，不在 relations.yaml 中定义，而是映射到具体的 PG 表（segment_tags 和 t_rst_relation）。

### 5.6 AggregateExecutor — 聚合执行器（含 rerank）

rerank 是最重要的 aggregate function，执行子阶段如下：

**Phase 4a — 特征打分：**

对每个候选段落计算特征向量：

| 信号 | 计算方式 | 权重（默认） |
|------|---------|-------------|
| source_rank | S=1.0, A=0.85, B=0.65, C=0.40 | 0.25 |
| confidence | segment.confidence 字段 | 0.20 |
| anchor_coverage | 命中的 expanded_node_set 节点数 / 总 expanded 节点数 | 0.25 |
| rst_coherence | 与已选段落的 RST 连续关系数 | 0.15 |
| freshness | 1.0 - (文档年龄月数 / 60)，下限 0.3 | 0.15 |

加权和 → `feature_score`。

**Phase 4b — Cross-encoder 精排：**

取 feature_score top-N（默认 50）段落，调用 LLM：

```
prompt: 
  你是一个相关性评估器。给定查询和文档段落，输出 0-10 的相关性分数。
  
  查询: {query}
  段落: {segment_text}
  
  只输出数字分数。
```

融合公式：`final_score = 0.4 × feature_score + 0.6 × (ce_score / 10)`

**Phase 4c — 预算感知选择：**

```python
selected = []
token_count = 0
for seg in sorted_by_final_score:
    if token_count + seg.tokens > budget:
        continue
    # RST 连贯性：如果 seg 的 RST 前驱已选中，优先选入
    selected.append(seg)
    token_count += seg.tokens
```

---

## 6. 保留边类型

HIN 中有 3 种边不在 `relations.yaml` 中定义，而是引擎内置的保留边类型：

| 保留边类型 | 连接 | 物理存储 |
|-----------|------|---------|
| `tagged_in` | OntologyNode → Segment | segment_tags 表 |
| `rst_adjacent` | Segment → Segment | t_rst_relation 表 |
| `evidenced_by` | Fact → Segment (via Evidence) | evidence 表 JOIN |

这些边在 expand 的 any_of 参数中可以直接引用，执行器识别到保留类型后自动切换到 PG SQL 查询而非 Neo4j Cypher。

---

## 7. API 设计

### 7.1 新增端点

```
POST /api/v1/query
Content-Type: application/json

{
  "name": "impact_analysis",
  "intent": "...",
  "steps": [...]
}
```

响应格式与现有算子一致：

```json
{
  "meta": {
    "ontology_version": "...",
    "latency_ms": 123,
    "steps_executed": 8,
    "steps_detail": [
      {"step": 0, "op": "seed", "as": "$ospf", "result_size": 2, "ms": 5},
      {"step": 1, "op": "expand", "as": "$blast", "result_size": 14, "ms": 45},
      ...
    ]
  },
  "result": {
    "$final": [...]
  }
}
```

### 7.2 与现有算子的关系

现有 21 个算子 REST API 保持不变（向后兼容）。内部实现逐步重写为构造查询计划 → 调用 QueryExecutor。对外 API 行为不变，但内部统一到同一执行引擎。

---

## 8. 查询校验的 JSON Schema

为了让 LLM 可靠地生成查询计划，每个原语的参数有严格的 JSON Schema。LLM 在 system prompt 中接收这个 schema 作为工具定义（function calling schema）。

```json
{
  "type": "object",
  "required": ["steps"],
  "properties": {
    "name": {"type": "string"},
    "intent": {"type": "string"},
    "steps": {
      "type": "array",
      "maxItems": 20,
      "items": {
        "type": "object",
        "required": ["op", "as"],
        "properties": {
          "op": {"enum": ["seed", "expand", "combine", "aggregate", "project"]},
          "as": {"type": "string", "pattern": "^\\$[a-z_][a-z0-9_]*$"}
        },
        "allOf": [
          {
            "if": {"properties": {"op": {"const": "seed"}}},
            "then": {
              "required": ["by", "value", "target"],
              "properties": {
                "by": {"enum": ["id", "alias", "layer", "embedding", "attribute"]},
                "value": {},
                "target": {"enum": ["node", "segment", "fact"]},
                "top_k": {"type": "integer", "minimum": 1, "maximum": 500},
                "threshold": {"type": "number", "minimum": 0, "maximum": 1}
              }
            }
          },
          {
            "if": {"properties": {"op": {"const": "expand"}}},
            "then": {
              "required": ["from"],
              "properties": {
                "from": {"type": "string", "pattern": "^\\$"},
                "any_of": {"type": "array", "items": {"type": "string"}},
                "sequence": {"type": "array", "items": {"type": "string"}},
                "direction": {"enum": ["outbound", "inbound", "both"], "default": "outbound"},
                "depth": {"oneOf": [{"type": "integer", "minimum": 1, "maximum": 10}, {"const": "unlimited"}], "default": 1},
                "confidence_decay": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "track_path": {"type": "boolean", "default": false},
                "target": {"enum": ["node", "segment", "fact"]}
              },
              "not": {
                "required": ["any_of", "sequence"]
              }
            }
          },
          {
            "if": {"properties": {"op": {"const": "combine"}}},
            "then": {
              "required": ["method", "sets"],
              "properties": {
                "method": {"enum": ["union", "intersect", "subtract"]},
                "sets": {"type": "array", "items": {"type": "string", "pattern": "^\\$"}, "minItems": 2}
              }
            }
          },
          {
            "if": {"properties": {"op": {"const": "aggregate"}}},
            "then": {
              "required": ["from", "function"],
              "properties": {
                "from": {"type": "string", "pattern": "^\\$"},
                "function": {"enum": ["count", "rank", "group", "score", "rerank"]},
                "by": {"type": "array", "items": {"type": "string"}},
                "signals": {"type": "array", "items": {"type": "string"}},
                "cross_encoder": {"type": "boolean", "default": false},
                "query": {"type": "string"},
                "order": {"enum": ["asc", "desc"], "default": "desc"},
                "budget": {"type": "integer"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 500}
              }
            }
          },
          {
            "if": {"properties": {"op": {"const": "project"}}},
            "then": {
              "required": ["from", "fields"],
              "properties": {
                "from": {"type": "string", "pattern": "^\\$"},
                "fields": {"type": "array", "items": {"type": "string"}, "minItems": 1}
              }
            }
          }
        ]
      }
    }
  }
}
```

---

## 9. 迁移策略

### 9.1 阶段一：引擎核心（不动现有 API）

实现 QueryValidator + QueryPlanner + QueryExecutor + 5 个原语执行器。新增 `POST /api/v1/query` 端点。现有 21 个算子 API 不变。

### 9.2 阶段二：算子内部重写

逐个将现有算子的过程式实现替换为查询计划构造 + QueryExecutor 调用。从最简单的开始：
1. `lookup` → seed(by=id)
2. `resolve` → seed(by=alias)
3. `expand` → seed + expand(depth=1)
4. `dependency_closure` → seed + expand(depth=6)
5. `impact_propagate` → seed + expand(depth=4, confidence_decay)
6. `context_assemble` → seed + expand(sequence) + expand(any_of=[tagged_in]) + aggregate(rerank) + project

### 9.3 阶段三：LLM agent 集成

在 agent 的 system prompt 中注入 JSON Schema，让 agent 动态生成查询计划。agent 不再调用固定算子 API，而是提交声明式查询。

---

## 10. 设计决策记录

| # | 问题 | 决策 | 理由 |
|---|------|------|------|
| 1 | rerank cross-encoder 模型 | **bge-reranker-v2**，WSL2 HTTP 服务 | 与 bge-m3 embedding 服务部署方式相同（systemd :8001），统一运维；延迟低（本地推理 ~50ms/pair）；无 API 费用 |
| 2 | 查询缓存 | **不缓存** | 图和文档持续变化，缓存一致性成本高于收益；当前图规模（~200 节点）实时查询足够快 |
| 3 | expand 物化视图 | **不物化，实时计算** | 当前本体规模 ~200 节点，BFS 6 跳在 Neo4j 中 <10ms；物化引入更新一致性问题，收益不明显。未来节点规模过万时再考虑 |
| 4 | 运行时安全边界 | **要有** | LLM 生成的查询可能触发笛卡尔积爆炸 |
| 5 | edges vs meta_path 命名 | **改为 `any_of` vs `sequence`** | 语义更明确，减少 LLM 混淆 |

### 10.1 Cross-encoder 服务架构

bge-reranker-v2 与 bge-m3 并行部署在 WSL2 中，端口分离：

```
WSL2 systemd 服务
├── bge-m3       :8000   ← embedding（已有）
└── bge-reranker :8002   ← reranking（新增）

应用服务（Windows）
└── FastAPI+Dashboard :8001  ← uvicorn（已有）
```

调用方式与 embedding 相同：HTTP POST，输入 (query, passage) 对，返回 relevance score。

AggregateExecutor 在 rerank 模式下，Phase 4b 调用 `RERANKER_HTTP_URL`（默认 `http://localhost:8002`），回退策略与 embedding 一致：HTTP 服务不可用时跳过 cross-encoder 阶段，仅用 Phase 4a 特征打分。

新增环境变量：

| 变量 | 说明 | 默认 |
|------|------|------|
| `RERANKER_HTTP_URL` | bge-reranker-v2 HTTP 服务地址 | `http://localhost:8002` |
| `RERANKER_ENABLED` | 是否启用 cross-encoder | `false` |

### 10.2 运行时安全边界

除 QueryValidator 的静态检查（§5.3）外，QueryExecutor 在运行时做动态守护：

| 守护项 | 限制 | 触发行为 |
|--------|------|---------|
| 中间结果集大小 | 单个 $variable 的节点数 ≤ 5000 | 截断并在 meta 中标记 `truncated: true` |
| expand 实际遍历节点数 | 累计访问 ≤ 10000 节点 | 提前终止 BFS，返回已收集结果 |
| 总执行时间 | ≤ 30 秒 | 超时返回部分结果 + `timeout: true` |
| 单步 Cypher/SQL | ≤ 5 秒 | 该步返回空 + 警告 |
| cross-encoder 调用量 | ≤ 50 对/次 | 仅对 top-50 调用，其余保留 feature_score |

### 10.3 expand 参数重命名

`edges` → `any_of`，`meta_path` → `sequence`。更新后的 expand schema：

```yaml
- op: expand
  from: $variable
  any_of: [depends_on, requires]      # OR 语义：沿任一类型的边走
  # 或
  sequence: [explains, implemented_by] # 序列语义：先走 A 再走 B
  direction: outbound
  depth: 4
  as: $result
```

JSON Schema 中的互斥约束相应更新：
```json
"not": {"required": ["any_of", "sequence"]}
```

---

### 10.4 reranker query 自动拼接

bge-reranker-v2 接受 (query, passage) 对。query 来源的优先级：

1. aggregate step 中显式指定的 `query` 字段
2. 查询计划顶层的 `intent` 字段
3. 自动拼接：取所有 seed step 的 `value` 字段，用空格连接

这样即使 LLM 生成的查询计划遗漏了 query/intent，reranker 也有可用的 query 文本。

### 10.5 expand depth: unlimited

`depth` 的取值：

- 正整数 `1`–`10`：固定深度递归
- 字符串 `"unlimited"`：BFS 直到不动点（visited 不再增长）

选择 `"unlimited"` 而非 `0` 或 `-1`，因为 LLM 更不容易把 `"unlimited"` 误用为"不遍历"。JSON Schema 中 depth 的类型为 `oneOf: [{integer, min:1, max:10}, {const: "unlimited"}]`。

运行时安全：unlimited 模式受 §10.2 的遍历节点上限（≤10000）保护，不会无穷展开。
