# 电信语义知识库 — 架构设计文档
**Python 实现（FastAPI + semcore 框架）**
**日期：2026-04-06 | 版本：1.0**

---

## 1. 这个系统要解决什么问题

在正式描述任何技术选型之前，必须先讲清楚这个系统存在的理由，因为所有架构决策都从这里推导出来。

### 1.1 电信知识的核心困境

电信领域有一个长期存在且被大量工具所忽视的问题：**同一个概念在不同厂商、不同标准组织的文档里叫法完全不同，但含义相同或高度重叠**。

- Cisco 文档里的 "OSPF neighbor"、Huawei 文档里的 "OSPF 邻居关系"、RFC 2328 里的 "OSPF adjacency"——三者描述的是同一件事。
- "BGP 路由反射器"（Huawei）= "Route Reflector"（Cisco）= "RR"（缩写）= "边界网关协议路由反射"（某些学术文档）。

当网络集成交付工程师做新建网络方案设计或动网割接方案时，他们需要跨越这道语言鸿沟，在脑子里手动完成术语归一化，才能把从不同厂商文档和标准中获取的知识拼在一起。这是一个认知负担极重、且非常容易出错的过程。

任何一个只做"检索相似文档"的工具都没有解决这个问题：它给你返回 5 条文档，其中 Cisco 的用英文术语，Huawei 的用中文术语，RFC 用规范术语，三条文档说的是同一件事，但你在看到它们的瞬间并不知道这一点。

### 1.2 为什么 RAG 不够

RAG（Retrieval-Augmented Generation）是当前最流行的知识处理方式，本质是"把知识访问的复杂性转嫁给 LLM"。这个模式有四个结构性缺陷：

**缺陷一：无法溯源到具体断言。** "BGP depends on TCP"——这个结论来自 RFC 4271、Cisco 技术手册、还是 LLM 的训练数据？置信度不透明，无法按来源权威性加权。

**缺陷二：无法做图遍历。** "OSPF 宕机会影响哪些服务？"需要沿着依赖边反向追踪。RAG 能找到 OSPF 相关文档，但无法告诉你依赖链。

**缺陷三：无法检测跨源冲突。** 两篇不同文档对同一事实给出矛盾说法，RAG 会把两条内容都返回给 LLM，LLM 可能随机选择其中一个，用户不会知道存在分歧。

**缺陷四：知识不会积累。** 处理了一千篇文档，RAG 的理解停留在"能检索"层面，不会随文档增加对某知识点产生更高置信度，也不会发现跨文档的知识模式。

本系统的目标是解决这四个问题：**可溯源、可遍历、冲突感知、知识积累**。

---

## 2. 系统定位的边界

明确系统**不是什么**，防止设计走偏：

| 对比维度 | RAG / 搜索引擎 | 本系统 |
|---|---|---|
| 知识单元 | 文档片段（chunk） | 结构化三元组（S-P-O）+ 来源溯源 |
| 归一化 | 无，依赖向量相似度 | 显式别名映射，跨厂商中英归一 |
| 查询类型 | 语义相似检索 | 图遍历 + 语义搜索 + 结构推理 |
| 来源追踪 | 文档级别 | 每条 fact 对应具体段落和文档 |
| 冲突处理 | 无 | 检测、记录、人工终审 |
| 知识演化 | 静态 | 候选词 → 评分 → 审核 → 入库 |
| 本体版本 | 无 | YAML 版本控制，Git 可追溯 |

---

## 3. 三条设计原则

所有架构决策都是这三条原则的体现：

**原则一：知识质量优先于知识数量。** 宁愿少存一条 fact，也不存一条低质量的 fact。一条错误的 "A depends_on B" 关系在故障扩散分析时会给出错误影响面，比没有数据更危险。

**原则二：真相来源只有一个，其他都是投影。** YAML 文件是本体的唯一真相。Neo4j 和 PostgreSQL 里的本体数据都是从 YAML 派生的运行时投影，可以随时用 `scripts/load_ontology.py` 重建。这是整个系统最重要的不变量。

**原则三：每个阈值和权重都有领域依据。** 置信度公式的权重、SimHash 阈值、候选词门控阈值——都不是随意选择的数字，背后都有针对电信文档特征的分析。如果要修改，先理解为什么是这个数字。

---

## 4. 分层架构：为什么是这六层

```
┌─────────────────────────────────────────────────────────────────┐
│  表现层   FastAPI REST + static/dashboard.html                   │
├─────────────────────────────────────────────────────────────────┤
│  算子层   21 个 SemanticOperator（src/operators/）               │
├─────────────────────────────────────────────────────────────────┤
│  治理层   ConfidenceScorer / ConflictDetector / EvolutionGate   │
│           OntologyMaintenance（src/governance/）                 │
├─────────────────────────────────────────────────────────────────┤
│  管线层   7 阶段 Pipeline（src/pipeline/stages/）                │
├─────────────────────────────────────────────────────────────────┤
│  框架抽象层  semcore ABCs                                        │
│   Stage / SemanticOperator / RelationalStore / GraphStore /     │
│   ObjectStore / ConflictDetector（零依赖，可独立发布）           │
├─────────────────────────────────────────────────────────────────┤
│  存储层   PostgreSQL(×2) + Neo4j + MinIO + Ollama               │
└─────────────────────────────────────────────────────────────────┘
```

### 4.1 为什么算子层和管线层必须分开

这是系统中最重要的架构切割线。

**Pipeline**（管线层）处理的是**文档加工**：拿到一篇原始文档，按顺序经过 7 个 Stage，产生结构化知识存入数据库。这是有状态的、有序的、单向的处理流，由 Worker 轮询触发，不接受外部 HTTP 请求。

**Operator**（算子层）处理的是**知识查询与操作**：基于已存储的知识执行语义操作，每个 Operator 是无状态的，接受 HTTP 请求返回 HTTP 响应。

如果混在一起会产生：算子依赖 PipelineContext 这个内部状态容器；Pipeline Stage 被外部请求触发导致并发问题；两者共享抽象导致参数签名混乱。分开之后，两层独立演化——加新算子不碰 Pipeline，改 Stage 不影响 API。

### 4.2 为什么设计 semcore 框架抽象层

semcore 是零依赖的 ABC 层（Python 标准库 ABC，没有任何第三方包）。它存在的核心原因：

**让 Pipeline Stage 依赖接口而不是实现。** 没有 semcore 时，Stage 代码会直接引用 `psycopg2.connect()` 或 `neo4j.GraphDatabase.driver()`，导致单元测试必须启动真实数据库，开发模式需要所有外部服务。

有了 semcore 的 `RelationalStore` ABC，Stage 代码只依赖接口，开发模式可以注入 `FakePostgres`（SQLite :memory:）和 `FakeNeo4j`（Python dict），单元测试零外部依赖。

**次要原因**：semcore 未来可以独立发布为 pip 包，供其他领域的语义知识库项目复用框架部分，而不引入任何电信领域的业务代码。

### 4.3 为什么没有单独的 Service 层

传统 Django/Flask 项目通常有 View → Service → Repository 三层。本系统选择 API Endpoint → Operator → Store，原因是：

Operator 本身就是"业务逻辑层"，它的粒度和 Service 相当。再封装一层 Service 只会增加文件数量而没有实质意义。每个 Operator 已经是一个内聚的业务单元（名字、参数、逻辑三位一体），不需要组合多个 Service。

### 4.4 为什么 app_factory.py 是唯一的组装入口

`build_app()` 是系统的**依赖注入根**（Composition Root）。所有 Provider 实例（PostgresRelationalStore、Neo4jGraphStore、ClaudeLLMProvider...）都在这里被实例化，然后注入到 `SemanticApp` 里。

好处：
- **依赖关系一目了然**：不需要在代码里到处 grep import 链，一个函数里就能看到整个系统依赖什么
- **方便替换**：开发模式只需要一个 `build_dev_app()` 函数，把所有 Provider 替换成 Fake 实现，业务代码一行不改
- **避免循环 import**：Stage 代码接受 `app` 参数而不是 import Provider，不存在 Stage → Provider → Stage 的循环

---

## 5. 五层本体模型：为什么是这五层

本系统的知识模型使用五层结构：概念（concept）→ 机制（mechanism）→ 方法（method）→ 条件（condition）→ 场景（scenario）。

### 5.1 为什么不用扁平本体（OWL 式）

扁平本体能回答"是什么"，但很难回答"在什么情况下用"和"为什么失败"。

例如：`BGP is-a routing-protocol` 是合法的扁平本体断言，但它无法表达：
- BGP **用什么机制**建立邻居关系（TCP 三次握手 + BGP OPEN 消息）→ 机制层
- **如何操作** BGP 邻居（`neighbor x.x.x.x remote-as` 命令）→ 方法层
- BGP 邻居在**什么条件**下会断开（Hold Timer 超时）→ 条件层
- BGP 邻居断开会触发**什么场景**（多 AS 边界路由黑洞）→ 场景层

这四个问题对应四个独立的认知框架，对不同角色有不同价值：集成交付工程师关心方法和场景（怎么配、什么场景用），方案设计师关心条件和机制（什么约束下选什么方案），架构师关心概念关系。扁平本体把这四类知识混在同一个层面，导致查询粒度无法控制。

### 5.2 为什么不用三层（TBox/RBox/ABox）

常见的 OWL 三层本体缺少对"方法"和"条件"的显式建模。在故障诊断场景中，"当前应该执行什么操作"（方法层）和"在什么触发条件下进入这个状态"（条件层）是完全不同的知识类型，混在一起会让查询变得模糊。

五层来自对电信集成交付知识结构的实际分析：一个有经验的交付工程师在做网络设计和割接方案时，脑子里同时维护这五类知识——概念对象间的依赖、背后的协议机制、具体的配置方法、适用条件和约束、以及目标业务场景——缺少任何一类都会导致方案不完整。

### 5.3 五层对应 Neo4j 五种节点标签的原因

每层对应一种 Neo4j 节点标签：OntologyNode / MechanismNode / MethodNode / ConditionRuleNode / ScenarioPatternNode。这是刻意设计：Neo4j 标签支持直接过滤，`MATCH (n:MechanismNode)` 不扫描其他层节点，查询性能与总节点量解耦。

---

## 6. 存储架构：为什么是这个组合

### 6.1 两个独立 PostgreSQL 而不是一个

`telecom_kb`（知识库）和 `telecom_crawler`（爬虫库）是完全独立的两个数据库，共享同一个 PostgreSQL 实例但 connection pool 隔离。

容易想到的替代方案是一个数据库两个 schema，为什么不这样做？

两个库有本质不同的特征：
- **知识库**：数据积累型，写入频率低查询频率高，数据需要长期保留，每条 fact 都有来源追溯价值。
- **爬虫库**：数据流转型，写入频率高，任务完成后数据价值降低，可以定期清理。

共用一个 schema，两者的 connection pool 会互相竞争——爬虫大量写入时，知识查询响应时间受影响。两库分开，connection pool 独立，互不影响。

更重要的是**故障隔离**：爬虫库不可用时（网络抖动、任务堆积），知识库的查询完全不受影响，21 个语义算子继续正常工作。

两库之间**没有外键约束**，这是刻意的。两库的关联通过应用层代码（UUID 传递）完成，而不是数据库约束。

### 6.2 为什么需要 Neo4j（不只用 PostgreSQL）

PostgreSQL 可以用递归 CTE 做图遍历：

```sql
WITH RECURSIVE closure AS (
    SELECT target FROM facts WHERE source = 'IP.BGP' AND predicate = 'depends_on'
    UNION ALL
    SELECT f.target FROM facts f JOIN closure c ON f.source = c.target
    WHERE f.predicate = 'depends_on'
) SELECT * FROM closure;
```

但这个查询在 facts 表数据量较大时性能很差——每一层递归都要扫描整个 facts 表做 JOIN，PostgreSQL 的 B-Tree 索引对这种模式优化有限。

Neo4j 的原生图存储在节点的物理层面保存了邻居指针（adjacency list），遍历邻居是 O(1) 而不是 O(log N) 的索引查找，适合**依赖闭包**（Dependency Closure）和**影响传播**（Impact Propagation）这类深度遍历操作。

另一个原因：**动态关系类型**。本系统的关系类型来自 LLM 抽取，是开放集合。PostgreSQL 没有"动态关系类型"的概念，每种类型需要一条 facts 记录加 predicate 字段区分；Neo4j 的关系类型是图的一等公民，可以直接在 MATCH 里过滤。

### 6.2.1 Neo4j 图数据模型——双层分离设计

Neo4j 中存在两个逻辑上分离的层面：**本体推理层**和**知识溯源层**。两层通过属性引用（而非图边）松耦合，这是刻意的架构决策。

```
┌─────────────────────────────────────────────────────────┐
│                    本体推理层                              │
│  用于图遍历（依赖闭包、影响传播、跨层推理）                    │
│                                                         │
│  OntologyNode ──[DEPENDS_ON]──> OntologyNode            │
│       │                              │                  │
│  [EXPLAINS]                     [IMPLEMENTED_BY]        │
│       │                              │                  │
│  MechanismNode ──[r]──> MethodNode ──[r]──> ...         │
│       ▲                                                 │
│  Alias ──[:ALIAS_OF]──> (任意本体节点)                    │
│                                                         │
│  边 = 多条 Fact 聚合的结论                                 │
│      属性：predicate, confidence(取最高), fact_count       │
└─────────────────────────────────────────────────────────┘
            ▲ 通过属性引用（f.subject = node.node_id），无图边
┌─────────────────────────────────────────────────────────┐
│                    知识溯源层                              │
│  用于证据追溯（这个结论从哪来）                              │
│                                                         │
│  Fact ──[:SUPPORTED_BY]──> Evidence                      │
│                              │                          │
│                    [:EXTRACTED_FROM]                     │
│                              │                          │
│                    KnowledgeSegment                      │
│                              │                          │
│                       [:BELONGS_TO]                      │
│                              │                          │
│                       SourceDocument                     │
└─────────────────────────────────────────────────────────┘
```

**为什么 Fact 不直接连接到 OntologyNode（无 ABOUT_SUBJECT/ABOUT_OBJECT 边）：**

1. **本体图保持干净**：OntologyNode 之间只有语义关系边（DEPENDS_ON、EXPLAINS 等），是多条 Fact 蒸馏后的聚合结论。图遍历（故障传播、依赖闭包）直接走本体边，不会被 Fact 节点打断路径。
2. **聚合语义**：3 条 Fact 说同一件事，在本体图上只产生 1 条边（`fact_count=3, confidence=max`）。如果每条 Fact 都有 ABOUT 边，647 个 Fact 会多出 1294 条边，本体图退化为 Fact 图。
3. **生命周期独立**：Fact 经历 `active → conflicted → superseded → merged` 状态流转，如果有边指向 OntologyNode，每次状态变更都要维护图边。属性引用让 Fact 生命周期管理完全在 PostgreSQL 侧完成，Neo4j 本体图不受影响。
4. **关注点分离**：推理查询（"BGP 故障影响什么"）只走本体推理层；溯源查询（"这个结论从哪来"）只走知识溯源层。两者互不干扰。

**查询示范**（`scripts/neo4j_queries.cypher` §2-B）：从一个本体节点出发，贯穿五层，展示链上每个节点的 Alias + Fact→Evidence→Segment→Document 完整证据链。

### 6.3 为什么用 MinIO

原始 HTML 和清洗后文本可能很大（单个 RFC 文档超过 1MB）。把这些内容存入 PostgreSQL 的 TEXT 字段会导致表膨胀、Vacuum 开销增大、备份体积过大。

MinIO 是对象存储，按 key 取内容延迟可预测，不影响关系型查询。Pipeline 处理时从 MinIO 取内容，处理完把结构化数据存 PostgreSQL，是标准的 Lambda 架构模式。

**内容去重**：`content_hash`（SHA-256）相同内容只存一份，不同 URL 爬来的相同页面不重复占用空间，这个去重在对象层面实现而不是数据库层面。

### 6.4 为什么用 pgvector 而不是独立向量数据库

向量检索有 Pinecone、Weaviate、Qdrant 等专用数据库。最终选择 pgvector 的核心原因：

**过滤条件和向量搜索总是同时出现。** 我们的语义搜索从来不是"在所有向量里找最近的"，而是"在 `lifecycle_state='active'` 且 `segment_type IN ('definition', 'config')` 的段落里找最近的"。这类混合查询在 pgvector 里是一个 WHERE 子句；在 Pinecone/Weaviate 里是向量搜索加后过滤（post-filter），实现复杂且性能不稳定。

**运维成本**。Pinecone 是 SaaS（网络依赖），Weaviate/Qdrant 是独立进程（额外运维）。pgvector 作为 PostgreSQL 扩展，和 PostgreSQL 共用连接池、事务、备份，零额外运维成本。

**数据规模**。我们的向量数量级在百万段落以内，pgvector 的 HNSW 索引完全够用。

### 6.5 为什么 governance 表使用独立 schema

`governance.evolution_candidates`、`governance.conflict_records`、`governance.review_records`、`governance.ontology_versions` 都在 governance schema 下，与公共表隔离。

原因：

**生命周期不同**。governance 表是工作队列性质的，候选词审核完成后可以归档或清理；而 facts、segments 是永久性知识记录，需要长期保留。

**访问控制**。未来可以在 schema 层面设置不同的 PostgreSQL role 权限，只有 governance 角色能操作候选词表，普通应用用户只能读写公共表。

**意图清晰**。`governance.evolution_candidates` 比 `evolution_candidates` 更清楚地表达这是"治理层面的元数据"而不是"知识本身"。

**开发模式兼容**：开发模式使用 SQLite，它不支持 schema 前缀。`FakePostgres` 在执行 SQL 时自动剥离 `governance.` 前缀，让 Stage 代码不需要区分开发/生产。

---

## 7. Pipeline 设计：为什么是 7 个阶段

### 7.1 为什么不是一个大步骤

直接把整个处理压缩成一步的想法很直观——给 LLM 发送原始 HTML，直接要求返回结构化 fact。为什么不这样做？

**LLM 上下文长度限制。** 一篇完整的 RFC 可能有 100 万 tokens，超过任何现有 LLM 的上下文窗口。分段（Stage 2）是必须的。

**故障可以定位到具体阶段。** `documents.status` 字段精确记录每篇文档卡在哪里（raw → cleaned → segmented → indexed），而不是一个模糊的"处理失败"。故障诊断时间从分钟级变成秒级。

**阶段可以独立重跑。** 如果 Stage 3 的别名匹配逻辑更新了，不需要重新下载和清洗所有文档，只需把 status 从 cleaned 回拨，重跑 Stage 3 之后的阶段。

**资源需求不同。** Stage 1 是 CPU 密集型（HTML 解析、哈希）；Stage 4 是 LLM API 调用密集型（网络 I/O，等待时间 5–30 秒）；Stage 3 是 Embedding 计算密集型（GPU/CPU 推理）。分开可以按需优化各阶段。

### 7.2 为什么 Stage 3b（Evolve）是独立的

Stage 3（Align）是每个文档独立执行的：处理一篇文档，发现一批候选词，打初始分，存库。

Stage 3b（Evolve）是批量执行的：每次执行加载所有状态为 discovered 的候选词（批次），基于最新的 source_count 重新计算评分，决定是否晋升或淘汰。

**关键区别**：一个候选词的 `source_count` 是累积的。"BGP-LS"第一次出现时 source_count=1，分数低，不会被晋升。当第 2、3 篇文档都提到它之后，source_count 升到 3，分数可能超过 0.70，触发自动晋升。这个逻辑需要看全局数据，不能在单文档的 Stage 3 里判断。

把 Evolve 放在每次 pipeline 循环里执行（而不是只在 Maintenance 里），是为了让高质量候选词**尽快晋升**，而不是等到半夜 3 点。Stage 3b 只做轻量的重新评分和状态流转，开销极低。

### 7.3 为什么 Stage 4 的 LLM 是硬要求

Stage 4 有三条路径：LLM 直接抽取 → 合并上下文重试 → 共现兜底。但如果 LLM 不可用，整个 pipeline 停止而不是降级。

**共现关系的语义价值极低。** 共现关系只能表达"A 和 B 在同一段文字里出现了"，谓语永远是 `co_occurs_with`，没有方向性，没有语义类型。这样的关系无法支撑依赖闭包、影响传播、故障推理——这些是整个系统的核心价值。

允许系统在 LLM 不可用时继续跑共现兜底会发生两件事：
1. 大量低质量 `co_occurs_with` 关系进入 Neo4j，后续所有图遍历结果质量下降
2. 工程师会误以为系统在正常工作，等 LLM 恢复后发现之前积累的数据需要大量清理

这两件事的代价远高于"停摆一段时间等 LLM 恢复"。共现关系**仍然保留**作为路径 3，是因为在 LLM 可用时，它作为补充记录"这两个本体节点在同一段落里共同出现"，从 S/A 级来源的共现 confidence 可以达到 0.74，是有意义的补充，不是降级。

### 7.4 为什么 Stage 4 有"合并上下文重试"这条路径

很多技术文档把一个事实分散在两个相邻段落里：
- 第一段（Elaboration）介绍概念："OSPF 使用 SPF 算法计算最短路径"
- 第二段（Sequence）补充细节："SPF 算法基于 Dijkstra，时间复杂度 O(V²)"

单独发送每个段落，LLM 可能因信息不完整而无法抽取有效三元组。合并后变成一个完整的语境，抽取成功率显著提升。

**为什么只对特定 RST 类型合并**：只有 Elaboration、Sequence、Restatement、Explanation 四种"连续型"关系才触发合并——因为这四种关系表明相邻段落在讲述同一件事。Contrast、Concession 等类型合并后反而引入混淆，因为它们讨论的是不同的事物。这个判断依赖 Stage 2 计算的 RST 关系结果。

---

## 8. 置信度评分：为什么是这个公式

```
confidence = 0.30 × source_authority
           + 0.20 × extraction_method
           + 0.20 × ontology_fit
           + 0.20 × cross_source_consistency
           + 0.10 × temporal_validity
```

**source_authority 权重最高（0.30）**：在电信领域，谁说的比怎么提取更重要。IETF RFC 经过严格标准化流程和多轮同行评审，错误率极低；论坛帖子的错误率远高于此。来源权威性放最高权重，反映了电信文档质量差距的现实。

| 等级 | 分值 | 典型来源 |
|---|---|---|
| S | 1.0 | IETF RFC、3GPP TS、ITU-T 建议书、IEEE 标准 |
| A | 0.85 | Cisco 技术手册、Huawei 产品文档、Juniper 官方文档 |
| B | 0.65 | 厂商白皮书、有权威作者的技术博客 |
| C | 0.40 | 论坛、问答社区、未知来源 |

**extraction_method 中 rule（0.85）高于 llm（0.70）**：这看起来反直觉，但有合理解释——精心为电信领域设计的规则对特定模式准确率极稳定（比如 "X MUST be Y" 是约束关系，几乎没有误判）；LLM 虽然覆盖范围广，但在边界案例上的不确定性更高。规则的高权重体现了"专门设计的规则比通用 LLM 更可靠"这一判断。

**temporal_validity 权重最低（0.10）**：电信标准生命周期非常长。RFC 793（TCP）发布于 1981 年至今有效。与 Web 内容不同，电信协议文档的陈旧风险远低于其他领域，时效性维度应该偏低。

**Neo4j 索引门控阈值 0.35 的来历**：这不是随意选择的数字。我们希望门控低于"最低质量但仍有意义"的 fact。从 S 级来源用共现兜底提取的 fact：`0.30×1.0 + 0.20×0.20 + 0.20×0.8 + 0.20×0.5 + 0.10×1.0 = 0.74`——远超门控。从 C 级来源的 LLM 抽取：`0.30×0.40 + 0.20×0.70 + 0.20×0.3 + 0.20×0.5 + 0.10×0.8 = 0.49`——也能进入 Neo4j。门控 0.35 主要是过滤完全错误的抽取结果（confidence 接近 0），而不是做精细筛选——精细筛选由 source_authority 权重自然完成。

---

## 9. 候选词治理：为什么是三层过滤

```
Stage 3（每篇文档）         粗筛：LLM 分类 + 停用词 + 初始评分
      ↓
Stage 3b（每次 pipeline 循环）  轻量重评分：基于累积 source_count 更新分数
      ↓
Maintenance（每天 03:00 CST）  深度精筛：embedding 聚类 + LLM 批量分类
      ↓
Human Review（随时）          终审：approve/reject/merge
```

**为什么 Stage 3 不直接决定是否入库？**

Stage 3 的视野只有当前这篇文档。它不知道"BGP-LS"这个词在过去一个月处理的 500 篇文档里出现了多少次，也不知道它是否是已有本体节点的变体。用单文档信息决定是否往本体加新节点，误入率会很高。

**为什么需要每天一次的 Maintenance？**

Stage 3b 做轻量评分（只重算分数和简单状态流转），可以每个 pipeline 循环执行。但有些操作太重，不适合频繁执行：

- **embedding 聚类去重**：把所有候选词做向量，找出相互 cosine > 0.85 的簇，合并同义候选词。这是 O(n²) 计算，1000 个候选词就是百万次向量乘法，不能每次 pipeline 都执行。

- **批量 LLM 分类**：对分数在 0.3–0.7 之间的候选词，每批 20 个发给 LLM 做"新概念/变体/噪声"的分类，需要几十个 API 调用。

这些放在 **03:00 CST** 的 Maintenance 窗口，原因是：凌晨 3 点是爬虫和 pipeline 的低谷期，不与常规工作竞争 CPU 和网络；03:00 CST = 19:00 UTC，也是欧美技术人员工作时间结束后，避免与外部 LLM API 高峰期重叠。

**为什么保留人工终审？**

自动化再完善，也会出现边界案例：某个候选词分数 0.72，但实际上是已有节点的缩写变体；两个候选词 embedding 相似度 0.88，不足以自动合并，但人工一看明显是同一概念。

人工终审不是"给机器打补丁"，而是**知识入库的最终责任边界**。自动化负责过滤明显噪声，人工负责处理机器无法判断的边界。

**Variant 合并的知识保留机制**：当一个候选词被判定为已有本体节点的变体（如"BGP4"是"BGP"的变体），不是简单删除——它出现过的文档段落会通过别名扩展被关联到正确节点（Stage 3 的 alias_map 增加这个别名，BackfillWorker 回填已有段落的 segment_tags）。知识不丢失，归位到正确节点。

---

## 10. LLM 集成设计

### 10.1 为什么 RST 关系使用闭合词表（21 种）

LLM 在没有约束时会发明谓语：`is_affected_by`、`impacts_performance_of`、`can_be_configured_via`... 谓语词表开放会导致 Neo4j 里出现数百种关系类型，图遍历变得不可控。

21 种 RST（修辞结构理论）关系类型来自语言学研究，覆盖了技术文档中常见的语篇关系。选择 RST 而不是自定义领域关系的原因：RST 类型是语言学意义上的关系，不是领域专有的，具有跨领域泛化性；`depends_on`、`requires`、`causes` 这些类型对电信领域已经足够表达力。

**但有例外**：当现有谓语都无法准确表达文本中的关系时，LLM **被允许创造新谓语**。这些新谓语会以 `candidate_type='relation'` 进入候选词池等待人工审核，而不是直接进入本体。这是"开放性 + 可控性"的平衡。

### 10.2 LLM 提示词为什么包含 nodeContext

Stage 4 在调用 LLM 前，先从 segment_tags 加载这个段落关联的本体节点，构成 nodeContext 放进提示词。

LLM 在有"线索"的情况下抽取质量更高——告诉 LLM "这段文本里出现了 BGP 和 TCP"，LLM 就不需要从零识别实体，可以集中精力判断它们之间的关系类型。这本质上是把 Stage 3 的对齐结果作为 Stage 4 的输入，是流水线信息传递的典型。

### 10.3 为什么只让 subject/object 使用本体节点 ID

LLM 提示词要求 subject 和 object **必须是**提供的节点 ID 列表中的 ID，不接受自由文本实体。

原因：如果允许 LLM 自由命名实体（如 `{"subject": "BGP router", "predicate": "uses", "object": "TCP"}` 而不是 `{"subject": "IP.BGP", ...}`），Neo4j 里会出现大量游离节点（"BGP router"、"Border Gateway Protocol Router"、"bgp-router" 这些可能是同一个东西），图会迅速变得混乱，图遍历结果不可信。

强制使用节点 ID 保证了所有 fact 都锚定在本体中，图结构保持清晰。

---

## 11. Embedding 架构

### 11.1 为什么选 bge-m3

bge-m3（BAAI/bge-m3）有三个特点符合本系统需求：
1. **中英双语**：电信文档中中英文混合极为普遍，双语模型比单语模型更准确
2. **1024 维**：维度足够高捕捉语义细节，不过高避免存储膨胀
3. **开源本地运行**：通过 Ollama 在本地推理，零 API 成本，无数据隐私问题

### 11.2 为什么 Ollama 优先、sentence-transformers 兜底

`src/utils/embedding.py` 的后端选择逻辑：

```python
# 启动时自动检测
if ollama_is_available():
    return OllamaEmbeddingClient()  # 无 Python 依赖、推理快、模型由 Ollama 管理
else:
    return SentenceTransformerClient()  # 需要 pip install，首次加载慢，但不依赖 Ollama
```

Ollama 的优势：不需要在 Python 进程里加载 PyTorch 和模型权重（节省内存），推理延迟低，模型通过 `ollama pull` 管理。

sentence-transformers 的存在原因：某些环境（如 CI/CD、轻量部署）没有 Ollama，需要有一个不依赖额外进程的兜底路径。两者的输出格式完全相同（float list），上层代码不感知差异。

### 11.3 Embedding 在哪些地方被使用，各用于什么目的

| 使用位置 | 用途 | 触发条件 |
|---|---|---|
| Stage 3 | 段落 vs 本体节点 embedding 匹配 | 精确别名匹配 0 命中时触发（兜底） |
| Stage 5 | 段落 embedding 相似度去重 | 总是执行（complement to SimHash） |
| Stage 5 | Fact 语义去重（S+O 相似） | embedding 可用时执行 |
| ConflictDetector | 检测语义冲突（S≈S, O≈O, P不同） | embedding 可用时执行 |
| Maintenance Pass 1 | 候选词聚类去重 | 每次 Maintenance 执行 |
| semantic_search 算子 | 查询向量 vs 段落向量 cosine 搜索 | 每次 API 请求 |
| similar_nodes 算子 | 本体节点 embedding 相似度计算 | 每次 API 请求 |
| OntologyQuality G维度 | 检测语义过近的兄弟节点 | 每次质量评估 |

所有场景的阈值：
- Stage 3 语义兜底匹配：cosine > 0.80
- Stage 5 段落去重：cosine > 0.90（比 Stage 3 更严，防止过度去重）
- ConflictDetector 语义冲突：自适应阈值（当前 facts 统计分布的第 90 百分位数）
- Maintenance 候选词聚类：cosine > 0.85

---

## 12. Worker 进程：4 线程协作模型

```
主进程 (worker.py)
├── _crawler_thread   — 持续爬取 pending 任务，写 raw 文档到 MinIO
├── _pipeline_thread  — 持续处理 raw 文档，走 7 阶段 pipeline
├── _stats_thread     — 每 5 分钟采集系统指标快照
└── _maintenance_thread — 每天 03:00 CST 执行本体维护
```

**线程间协作方式：数据库状态而不是共享内存**

```
crawler_thread 写 documents(status='raw')
                         ↓
pipeline_thread 读 status='raw' 并处理
                         ↓
pipeline_thread 写 evolution_candidates
                         ↓
maintenance_thread 读 evolution_candidates 并归类
```

线程之间没有直接通信（没有 Queue、没有 Event 信号），完全通过数据库状态间接协调。好处：任何一个线程崩溃和重启都不影响其他线程的正常运行；重启后从数据库当前状态续跑，不丢失进度。

**为什么使用 threading 而不是 asyncio 或 multiprocessing**

pipeline 处理的瓶颈是 I/O（LLM API 调用 5–30 秒、MinIO 读写、Neo4j 查询），不是 CPU。Python 的 GIL 对 I/O 密集型线程没有实质性限制——`time.sleep()` 和网络 I/O 时 GIL 会自动释放。

asyncio 的方式代码改写成响应式风格，复杂度高，PSYCOPG2（同步 PostgreSQL 驱动）不天然支持，需要引入 asyncpg 和大量 `async/await`。

multiprocessing 则需要处理进程间通信（Queue、Pipe、共享内存），overhead 更大，不必要。

4 个 daemon 线程通过 `threading.Event` 协调优雅退出：Ctrl+C 设置 `_stop_event`，所有线程在下一次循环检测到后自行结束，不需要强制 kill。

**_pipeline_thread 对 LLM 的双重检查**：不只是在 pipeline 开始时检查一次，而是每篇文档处理前都重新检查。原因是 LLM 可能在处理一批文档的中途变得不可用（网络抖动、API 限流），及时检测可以避免把后续文档以降级模式处理。

---

## 13. 模式外部化：为什么正则放在 YAML 里

所有正则匹配模式存放在 `ontology/patterns/` 下的 YAML 文件：
- `semantic_roles.yaml`：22 种语义角色的关键词模式
- `context_signals.yaml`：6 种上下文信号
- `predicate_signals.yaml`：13 种谓语信号
- `candidate_stopwords.yaml`：候选词停用词表

**为什么不硬编码在 Python 代码里？**

本体知识和代码逻辑必须分离。当网络工程师说"我觉得'IS-IS'相关的段落应该被识别为配置类型"，他修改的应该是 YAML，而不是需要找工程师改代码然后部署。

把模式放在 YAML 还有另一个好处：OntologyRegistry 支持热重载（`POST /reload_ontology`），修改 YAML 后无需重启应用即可生效，这对频繁迭代本体的团队非常重要。

**候选词停用词**的例子说明这有多重要：电信文档里"configuration"、"network"、"protocol"这些词出现频率极高，但它们是通用词，不是新概念。没有停用词表，候选词池会被这些词填满。停用词表放在 YAML 里，领域专家可以直接维护，不需要工程师介入。

---

## 14. 开发模式：如何在没有外部服务时工作

`python run_dev.py` 启动一个完全在内存里运行的开发环境：

```python
# run_dev.py 的核心
app = build_dev_app()  # 注入所有 Fake 实现
# FakePostgres:   SQLite :memory:（自动剥离 governance. 前缀）
# FakeNeo4j:      Python dict 图
# FakeMinio:      内存字典
# FakeLLM:        返回预置的假 fact（用于 pipeline 测试）
# FakeEmbedding:  返回随机向量（用于 embedding 相关逻辑测试）
seed_dev_data(app)  # 从 YAML 本体初始化测试数据
```

**这意味着**：新工程师克隆代码后，不需要安装任何外部服务，直接 `python run_dev.py` 就能看到系统运行（Dashboard、所有 API 端点都可用），极大降低了开发环境搭建成本。

开发模式的限制：FakeNeo4j 只支持基本 MATCH/MERGE，不支持复杂路径查询（如 shortestPath）；FakeLLM 返回固定结果，不会随文本内容变化。真实的 LLM/Neo4j 能力需要在真实服务上测试。

---

## 15. 整体设计取舍总结

| 决策 | 选择 | 放弃的选项 | 核心原因 |
|---|---|---|---|
| 知识单元 | S-P-O 三元组 + 来源 | 文档向量块 | 支持图遍历、来源溯源、冲突检测 |
| 本体存储 | YAML + Git | 直接存 Neo4j | 可版本控制、可 diff、可回滚、多环境复现 |
| 图数据库 | Neo4j | 只用 PostgreSQL | 深度图遍历（依赖闭包、影响传播）性能 |
| 向量存储 | pgvector | Pinecone / Weaviate / Qdrant | 避免新基础设施；混合过滤查询更简单 |
| 并发模型 | threading（4 线程） | asyncio / multiprocessing | I/O 密集型，GIL 无限制；代码最简单 |
| LLM 接入 | OpenAI 兼容 httpx | LangChain / LlamaIndex | 轻量；切换厂商只改 base_url |
| Embedding | Ollama 优先 + sentence-transformers 兜底 | OpenAI embedding API | 本地推理零成本；无 API 依赖 |
| LLM 可用性 | 硬要求（停止 pipeline） | 降级到共现兜底 | 知识质量不能为可用性妥协 |
| 候选词过滤 | 三层（Stage3+Stage3b+Maintenance+人工） | 纯自动 | 边界案例需要领域专家判断 |
| 维护窗口 | 每天 03:00 CST | 随时触发 | 不与爬取/pipeline 高峰期冲突 |
| 数据库分离 | 双 PostgreSQL | 单库多 schema | Connection pool 隔离；故障不传播 |
| 框架抽象 | semcore ABCs | 直接依赖具体实现 | 开发模式零外部依赖；Stage 可独立测试 |
| 正则模式 | YAML 外部化 | 硬编码在代码里 | 领域专家可直接维护；支持热重载 |
| 来源权威性 | source_authority 权重最高 (0.30) | 平等权重 | 电信领域来源质量差距显著 |