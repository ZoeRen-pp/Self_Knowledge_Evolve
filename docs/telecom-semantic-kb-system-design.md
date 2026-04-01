
# 网络通信领域语义知识库系统设计文档

**版本**：v0.1  
**状态**：设计初稿  
**定位**：面向网络通信领域公开知识的受控采集、精炼、对齐、存储、检索、推理与本体演化的语义知识基础设施  

---

# 1. 文档目标

本文档定义一套面向网络通信领域的语义知识库系统设计方案。该系统以领域本体为稳定语义骨架，以公开网页语料为知识来源，通过知识抽取、知识治理、多模存储和语义操作算子，构建可检索、可追溯、可演化、可约束的通信领域语义知识底座。

系统目标不是简单做“全文搜索”或“网页抓取”，而是构建一套具备以下能力的知识基础设施：

1. 对网络通信领域知识进行统一语义组织。
2. 将公开网页中的非结构化知识转化为可计算知识对象。
3. 通过本体实现目录、索引、检索和关系导航。
4. 通过语义算子支撑上层检索、问答、推理和分析应用。
5. 允许领域知识随时间扩展，但通过受控演化防止本体发散。

---

# 2. 总体设计原则

## 2.1 稳定骨架与动态知识分离

系统将“稳定语义结构”和“动态增长知识”分离：

- **本体层**承担稳定骨架，定义概念、关系、层次和约束。
- **知识层**承载从语料中抽取出的事实、段落、证据、别名和上下文。
- **候选层**承载新概念和新关系的观察结果，不直接污染核心本体。

## 2.2 图结构与文本载体分离

图数据库主要保存：

- 本体节点
- 概念/实体
- 关系
- 事实
- 证据索引
- 标签关联

原始文档、清洗文本和分片文本不作为图数据库主载荷，而由对象存储 / 文档库承载。

## 2.3 事实去重优先于文本去重

系统最终治理对象是“事实”，而不是“文本形式”。  
同一事实在多个来源、多个表述中出现时，应归并为一个规范事实，并附上多源证据链。

## 2.4 来源可信度分层

不同来源对知识主库的贡献权重不同。系统采用来源分级策略：

- S级：标准组织、正式规范
- A级：主流厂商官方文档
- B级：高质量技术文章、教材、公开课程
- C级：论坛、博客、问答社区

低等级来源可作为辅助证据，不应直接驱动核心本体或高可信事实。

## 2.5 本体演化受控而非实时漂移

系统允许发现新概念，但不允许本体被噪声驱动频繁变化。  
新概念必须先进入候选区，经稳定性、跨源一致性、结构适配性评估后，再并入领域本体。

---

# 3. 适用范围

本系统面向网络通信领域知识，包括但不限于：

- 数通网络
- 光网络
- 接入网
- 承载网
- 核心网
- 网络运维与管理
- 网络自动化与配置
- 告警、故障与排障知识
- 协议机制、部署实践、约束条件、性能指标

系统首版建议从 **IP/数通子域** 或 **运维故障子域** 先落地，再逐步扩展至全域。

---

# 4. 目标能力

## 4.1 基础能力

1. 管理网络通信领域本体及其版本。
2. 对公开网页和公开技术文档进行分层采集。
3. 进行文本清洗、结构识别和语义切分。
4. 对知识片段进行本体对齐、标签标注和关系抽取。
5. 完成知识去重、融合、证据绑定和可信度评估。
6. 支持图检索、语义检索、证据追溯和关系导航。

## 4.2 高阶能力

1. 支持语义依赖分析。
2. 支持故障影响传播和根因知识追踪。
3. 支持本体候选概念发现和受控演化。
4. 支持上层问答、Agent、方案生成、配置理解等应用接入。

---

# 5. 非目标

系统初版不直接追求：

1. 全网无边界爬取。
2. 全自动无监督本体演化。
3. 单一数据库承载所有文本、图谱和向量能力。
4. 无证据约束的“知识自动生成”。
5. 直接替代标准文档或厂商原始文档。

---

# 6. 总体架构

系统采用六层架构：

1. 本体核心层
2. 语料接入层
3. 知识加工层
4. 多模存储层
5. 语义操作层
6. 本体演化与治理层

## 6.1 架构说明

### 6.1.1 本体核心层

负责维护领域语义骨架，包括：

- 顶层概念体系
- 子域概念体系
- 关系类型体系
- 约束与规则
- 术语别名
- 版本管理
- 演化策略

### 6.1.2 语料接入层

负责采集和标准化公开网页语料，包括：

- 站点白名单管理
- URL种子管理
- 页面抓取
- HTML/PDF/Markdown解析
- 页面正文抽取
- 页面结构识别
- 元数据提取

### 6.1.3 知识加工层

负责把文档转化为可计算知识：

- 语义切分
- 本体标签匹配
- 术语归一
- 实体识别
- 关系抽取
- 事实构造
- 可信度打分
- 去重融合
- 证据链绑定

### 6.1.4 多模存储层

由多种存储协同组成：

- PostgreSQL：元数据、任务、规则、版本
- 对象存储：原始网页、清洗文本、附件、快照
- 图数据库：本体、实体、关系、事实、证据索引
- 向量索引：片段向量、检索召回

### 6.1.5 语义操作层

通过语义算子向上提供统一访问接口，而非让上层直接操作底层库。

### 6.1.6 本体演化与治理层

负责新概念发现、候选评估、结构适配、版本审计和受控发布。

---

# 7. 技术架构建议

## 7.1 存储选型建议

### PostgreSQL

> **v0.3 更新**：PostgreSQL 已拆分为两个数据库 + 两个 schema：
> - **telecom_kb**（知识库）：`public` schema 存知识数据（documents, segments, facts, evidence, segment_tags, lexicon_aliases）；`governance` schema 存治理数据（evolution_candidates, conflict_records, review_records, ontology_versions）
> - **telecom_crawler**（爬虫库）：存爬虫调度数据（source_registry, crawl_tasks, extraction_jobs）
>
> 原 `t_edu_detail` 表已合并入 `segments` 表（新增 title/title_vec/content_vec/content_source 列）。

用途：

**知识库 telecom_kb**：
- 文档元数据（documents）
- 知识片段及嵌入向量（segments，含 EDU 标题/向量）
- 片段标签（segment_tags）
- 事实与证据（facts, evidence）
- 词汇别名（lexicon_aliases）
- RST 语篇关系（t_rst_relation）

**知识库 governance schema**：
- 本体版本管理（ontology_versions）
- 候选概念（evolution_candidates）
- 冲突记录（conflict_records）
- 审核记录（review_records）

**爬虫库 telecom_crawler**：
- 来源注册（source_registry）
- 爬取任务（crawl_tasks）
- 流水线任务追踪（extraction_jobs）

### 图数据库

建议首版采用 **Neo4j**：

- 建模直观
- 关系查询方便
- 适合知识图谱原型构建和快速迭代

后续规模扩大可评估 NebulaGraph 或 JanusGraph。

### 向量库

首版可采用：

- PostgreSQL + pgvector

后续可替换为：

- Qdrant
- Milvus

### 对象存储

建议采用：

- MinIO 或兼容 S3 的对象存储

存储内容：

- 原始网页HTML
- 解析后Markdown
- 清洗文本
- PDF
- 页面截图
- 语义切分结果快照

## 7.2 处理框架建议

- Python 作为主处理语言
- Airflow / Prefect 作为调度编排
- Scrapy / Playwright 用于网页获取
- trafilatura / readability-lxml / 自研规则用于正文抽取
- spaCy / 自定义NER / LLM辅助抽取用于语义识别
- LLM仅作为抽取增强器，不作为唯一事实来源

---

# 8. 数据源设计

## 8.1 来源分级

### S级来源
- IETF
- 3GPP
- ITU-T
- IEEE
- ETSI
- MEF
- TM Forum
- ONF

### A级来源
- Cisco
- Huawei
- Juniper
- Nokia
- Ericsson
- H3C
- Arista
- Ciena
- 主流云厂商网络技术文档

### B级来源
- 高质量公开课程
- 技术白皮书
- 高质量技术社区文章

### C级来源
- 论坛
- 博客
- 问答社区

## 8.2 采集策略

采用“领域白名单 + 主题路由”的采集模式，而不是无边界全网抓取。

采集过程包括：

1. 种子URL初始化
2. 站点结构发现
3. 页面抓取
4. 页面类型识别
5. 页面去模板与正文抽取
6. 文档版本检测
7. 增量更新

## 8.3 页面类型识别

不同页面类型采用不同处理策略：

- 标准规范页
- 产品说明页
- 配置手册页
- FAQ页
- 教学文章页
- 下载附件页
- PDF页

---

# 9. 知识对象模型

系统中引入以下核心对象。

## 9.1 SourceDocument

表示一个来源文档。

字段建议：

- source_doc_id
- source_url
- canonical_url
- site_name
- source_rank
- title
- doc_type
- language
- publish_time
- crawl_time
- content_hash
- normalized_hash
- version_hint
- raw_storage_uri
- cleaned_storage_uri
- status

## 9.2 KnowledgeSegment

表示经过语义切分后的知识片段，是最小入库语义单元。

字段建议：

- segment_id
- source_doc_id
- section_path
- section_title
- segment_index
- segment_type
- raw_text
- normalized_text
- token_count
- ontology_tags
- semantic_role_tags
- context_tags
- confidence
- dedup_signature
- embedding_ref
- evidence_ref

## 9.3 Concept / Entity

- Concept：抽象概念，例如 BGP、OTN、AMF
- Entity：实例对象，例如某个设备、某条链路、某个接口

## 9.4 Fact

表示规范化事实，例如：

- BGP uses TCP
- OSPF adjacency requires matching area
- EVPN uses BGP as control plane

字段建议：

- fact_id
- subject
- predicate
- object
- qualifier
- domain
- confidence
- lifecycle_state
- merge_cluster_id

## 9.5 Evidence

表示支撑事实的证据。

字段建议：

- evidence_id
- fact_id
- source_doc_id
- segment_id
- exact_span
- source_rank
- extraction_method
- evidence_score

## 9.6 CandidateConcept

表示尚未进入正式本体的候选概念。

字段建议：

- candidate_id
- surface_forms
- candidate_parent
- structural_fit_score
- temporal_stability_score
- source_diversity_score
- adoption_score
- review_status

---

# 10. 标签体系设计

系统中的标签采用三层设计。

## 10.1 Canonical Tag

严格映射到本体节点，承担主目录索引作用。  
例如：

- BGP
- MPLS
- OTN
- AMF
- OLT

## 10.2 Semantic Role Tag

表示该片段的语义功能。  
例如：

- 定义
- 组成
- 工作机制
- 配置方法
- 故障现象
- 排障步骤
- 性能指标
- 约束条件
- 风险
- 最佳实践

## 10.3 Context Tag

表示适用上下文。  
例如：

- 园区网
- 城域网
- 数据中心
- 承载网
- 接入网
- 5GC
- 多厂商组网

## 10.4 标签约束

- Canonical Tag 必须从本体节点集中选取。
- Semantic Role Tag 从受控枚举中选取。
- Context Tag 从受控上下文词表中选取。
- 一个片段可以对应多个标签，但必须有至少一个 Canonical Tag。
- 标签必须支持版本化，以适应本体升级后的重对齐。

---

# 11. 知识加工流水线

## 11.1 流水线阶段划分

### 阶段1：采集与标准化
- 获取原始页面
- 页面去模板
- 正文抽取
- 结构识别
- 元数据提取

### 阶段2：语义切分
- 按标题层级切分
- 按表格、命令块、列表块切分
- 按语义角色切分复合段落

### 阶段3：语义识别
- 术语识别
- 别名归一
- 本体对齐
- 实体识别
- 关系抽取
- 语义角色识别
- 上下文识别

### 阶段4：知识构造
- 事实构造
- 证据绑定
- 可信度打分
- 冲突初筛

### 阶段5：知识治理
- 页面级去重
- 段落级去重
- 事实级去重
- 多源融合
- 冲突显式标注

### 阶段6：入库与索引
- 文档入对象存储
- 元数据入 PostgreSQL
- 图谱对象入图数据库
- 向量入向量索引

---

# 12. 语义切分设计

## 12.1 为什么不能只按长度切分

如果只按 token 长度切分，会导致：

- 段落语义边界被破坏
- 定义、机制、案例混在一起
- 标签难以准确映射
- 事实抽取噪声增加

## 12.2 推荐切分策略

优先采用“结构优先 + 语义优先”的混合切分。

### 结构切分
按以下元素进行初切：

- H1/H2/H3 标题
- 列表项
- 表格
- 配置块
- 日志块
- 代码块
- 命令块

### 语义切分
对于单段内同时包含多个语义角色的文本，再做细切：

- 定义段
- 原理段
- 约束段
- 配置段
- 故障段
- 排障段

## 12.3 Segment类型建议

- definition
- mechanism
- constraint
- config
- example
- fault
- troubleshooting
- best_practice
- performance
- comparison

---

# 13. 术语识别与本体对齐

## 13.1 识别对象

- 协议名
- 网络对象名
- 接口类型
- 功能实体
- 告警名
- 指标名
- 配置对象名
- 厂商术语
- 缩写

## 13.2 归一层次

### 词面归一
例如：
- Border Gateway Protocol → BGP
- Interior Gateway Protocol → IGP

### 厂商术语归一
例如：
- 不同厂商对相似特性的命名差异

### 版本语义归一
例如：
- 5GC中的某些功能实体在不同版本中的差异表达

## 13.3 本体对齐原则

1. 优先匹配 Canonical Node。
2. 若未命中，尝试别名层。
3. 若仍未命中，进入候选概念池。
4. 不允许无约束新增核心节点。

---

# 14. 关系抽取与事实生成

## 14.1 关系抽取目标

从文本中抽取可规范表达的关系，例如：

- `BGP uses TCP`
- `OSPF adjacency requires area match`
- `EVPN uses BGP`
- `LACP aggregates interfaces`
- `AMF interacts_with SMF`

## 14.2 关系类型分层

### 分类关系
- is_a
- part_of
- instance_of

### 结构关系
- contains
- connects_to
- hosted_on
- mounted_on

### 功能关系
- uses_protocol
- implements
- establishes
- advertises
- forwards_via
- encapsulates

### 依赖关系
- depends_on
- requires
- precedes
- conflicts_with
- constrains

### 运维关系
- raises_alarm
- impacts
- causes
- mitigated_by
- verified_by
- configured_by

### 证据关系
- supported_by
- derived_from
- mentioned_in
- contradicted_by

## 14.3 事实生成规则

事实生成必须满足：

1. 主语和宾语语义边界清晰。
2. 谓词属于受控关系类型集合。
3. 事实应具备证据来源。
4. 抽取可信度需量化。
5. 可在后续去重融合阶段归并。

---

# 15. 去重与融合机制

## 15.1 页面级去重

基于：

- canonical URL
- 文本哈希
- 去模板后的正文哈希

## 15.2 段落级去重

基于：

- SimHash / MinHash
- 向量相似度
- 标题 + 文本联合签名

## 15.3 事实级去重

基于规范事实三元组：

- subject
- predicate
- object

并考虑 qualifier、适用条件和版本约束。

## 15.4 多源融合

当多个来源支撑同一事实时，应合并为一个规范事实，并附加多个 Evidence。

## 15.5 冲突检测

当不同来源表述冲突时：

- 保留冲突双方
- 标明来源等级
- 标明时间维度
- 标明适用上下文
- 不强行合并

---

# 16. 可信度设计

## 16.1 可信度组成因素

建议采用如下因素综合打分：

- 来源等级
- 文本抽取质量
- 本体对齐置信度
- 关系抽取置信度
- 多源一致性
- 时间新鲜度
- 是否被高等级来源支持

## 16.2 示例评分公式

可定义：

`Confidence = w1*SourceAuthority + w2*ExtractionQuality + w3*OntologyFit + w4*CrossSourceConsistency + w5*TemporalValidity`

其中各权重可配置。

---

# 17. 多模存储设计

## 17.1 PostgreSQL

> **v0.3 更新**：已拆分为两个数据库，知识库内再分两个 schema。

**知识库 telecom_kb — public schema**：

- documents（文档元数据）
- segments（知识片段 + EDU 标题/向量，原 t_edu_detail 已合并）
- segment_tags（片段本体标签）
- facts（三元组知识）
- evidence（事实溯源）
- lexicon_aliases（词汇别名）
- t_rst_relation（RST 语篇关系，21 种通用类型）

**知识库 telecom_kb — governance schema**：

- governance.ontology_versions（本体版本）
- governance.evolution_candidates（候选概念）
- governance.conflict_records（冲突记录）
- governance.review_records（审核记录）

**爬虫库 telecom_crawler**：

- source_registry（来源注册）
- crawl_tasks（爬取任务）
- extraction_jobs（流水线任务追踪）

## 17.2 图数据库

主要节点类型：

- OntologyClass
- OntologyRelation
- Concept
- Entity
- Fact
- KnowledgeSegment
- SourceDocument
- Evidence
- Alias
- CandidateConcept
- Rule
- Version

主要边类型：

- SUBCLASS_OF
- INSTANCE_OF
- PART_OF
- RELATED_TO
- DEPENDS_ON
- REQUIRES
- USES
- IMPACTS
- CAUSES
- SUPPORTED_BY
- EXTRACTED_FROM
- TAGGED_AS
- ALIAS_OF
- CONTRADICTED_BY

## 17.3 向量索引

向量对象主要为：

- segment embedding
- fact textual representation embedding
- concept description embedding

---

# 18. 语义操作算子设计

语义操作层是系统对上提供能力的核心接口。

## 18.1 基础算子

### `semantic_lookup(term, scope, version)`
用途：
- 查询术语
- 解析别名
- 返回本体节点、定义、相关证据

### `semantic_expand(node, relation_types, depth)`
用途：
- 围绕某个概念扩展关联知识
- 按指定关系类型和深度获取邻域

### `semantic_filter(objects, constraints)`
用途：
- 按来源等级、时间、域、厂商、可信度过滤

### `semantic_resolve(alias)`
用途：
- 将别名、缩写、厂商术语映射为规范概念

## 18.2 关系算子

### `path_infer(start, end, relation_policy)`
用途：
- 发现两个概念间的语义路径

### `dependency_closure(node)`
用途：
- 求取配置、协议或机制的依赖闭包

### `impact_propagate(event_node, policy)`
用途：
- 从故障或事件出发做影响扩散

## 18.3 证据算子

### `evidence_rank(fact)`
用途：
- 对支撑同一事实的证据排序

### `fact_merge(fact_candidates)`
用途：
- 合并候选事实为规范事实

### `conflict_detect(topic)`
用途：
- 检测同一主题下的冲突知识

## 18.4 演化算子

### `candidate_concept_discover(window)`
用途：
- 在一段时间窗口内发现潜在新概念

### `ontology_attach_score(candidate)`
用途：
- 评估候选概念接入哪一父节点最合理

### `evolution_gate(candidate)`
用途：
- 基于规则与评分判断是否进入审核流程

---

# 19. 本体演化机制

## 19.1 分层演化结构

### Core Ontology
- 顶层类
- 基础关系
- 长期稳定概念
- 仅允许人工审批修改

### Domain Ontology
- 子域概念
- 中低频演化
- 允许半自动建议 + 人审

### Lexicon / Alias Layer
- 缩写
- 别名
- 厂商术语
- 高频自动更新

## 19.2 候选概念来源

- 新抓取文档中的高频新术语
- 多源一致出现但无法对齐的概念
- 关系抽取中重复出现的未收录宾语/主语
- 厂商新产品、新协议扩展、新框架术语

## 19.3 候选概念评分建议

可以综合以下因子：

- SourceAuthority
- SourceDiversity
- TemporalStability
- StructuralFit
- RetrievalGain
- NonSynonymProbability

## 19.4 防发散约束

1. 不实时写入核心本体。
2. 不允许候选概念无父节点落入正式层。
3. 不允许别名直接升级为概念。
4. 同一演化周期设置变更配额。
5. 本体必须版本化，支持差异审计和回滚。
6. 对新节点进行影响分析后再发布。

---

# 20. 治理与审计机制

## 20.1 审计对象

- 数据源接入
- 抽取规则版本
- 事实生成记录
- 置信度变化
- 本体变更记录
- 冲突处理记录

## 20.2 回溯能力

每个规范事实都应能追溯到：

- 来源文档
- 原始段落
- 抽取方法
- 本体版本
- 合并历史

## 20.3 数据生命周期

- active：当前有效
- superseded：被更新替代
- deprecated：不再推荐
- conflicted：存在冲突
- pending_review：待审核

---

# 21. 质量保障机制

## 21.1 评测维度

### 本体层
- 层级完整性
- 父子关系一致性
- 关系约束正确性

### 抽取层
- 术语识别准确率
- 本体对齐准确率
- 关系抽取准确率
- 事实规范化准确率

### 检索层
- 召回率
- 精确率
- 证据可追溯率

### 演化层
- 候选概念命中率
- 错误演化率
- 演化收益

## 21.2 质量控制策略

- 引入黄金样本集
- 引入多来源对照集
- 对核心子域进行人工抽检
- 对高风险关系类型采用更强规则约束

---

# 22. 安全与合规约束

1. 仅采集允许公开访问的内容。
2. 遵循网站 robots 和使用条款。
3. 记录来源和版权归属信息。
4. 系统内部保存的是知识索引与证据引用，不用于复制分发受版权保护全文。
5. 支持来源删除与禁采策略。

---

# 23. MVP落地建议

## 23.1 首期范围建议

优先选取 **IP/数通子域**，覆盖：

- Ethernet
- VLAN
- STP/MSTP
- LACP
- VRRP
- OSPF
- IS-IS
- BGP
- MPLS
- EVPN
- QoS
- ACL
- NAT

## 23.2 首期数据源

- IETF RFC
- Cisco 官方文档
- Huawei 官方文档
- Juniper 官方文档
- 少量高质量技术文章

## 23.3 首期能力清单

1. 本体版本管理
2. 白名单采集
3. 文档清洗
4. 章节/段级切分
5. Ontology Tag 标注
6. 基础关系抽取
7. 事实与证据入图
8. 基础语义检索
9. 候选概念发现但不自动生效

---

# 24. 推荐实施阶段

## Phase 1：本体与规则先行
- 定义顶层本体
- 定义数通子域本体
- 定义关系类型
- 定义标签体系
- 定义抽取规则框架

## Phase 2：知识接入流水线
- 建立采集白名单
- 打通文档清洗、切分、标注和入库

## Phase 3：知识治理
- 实现去重融合、证据链、冲突显式化

## Phase 4：语义算子层
- 实现 lookup、expand、dependency、impact、evidence

## Phase 5：本体演化闭环
- 候选发现
- 挂接评分
- 审核发布
- 版本回滚

## Phase 6：应用接入
- 问答
- 检索
- 配置理解
- 故障分析
- 方案生成辅助

---

# 25. 风险点与应对策略

## 风险1：语料质量差导致噪声进入主库
应对：
- 严格来源分级
- 设置可信度阈值
- 低等级来源只做辅助证据

## 风险2：文本切分不合理导致语义污染
应对：
- 结构切分 + 语义切分结合
- 对复合段落做再分解

## 风险3：本体演化过快导致结构漂移
应对：
- 候选区隔离
- 版本审计
- 配额控制
- 人审门控

## 风险4：厂商知识与标准知识混杂
应对：
- 增加来源维度和上下文维度
- 将厂商实现差异显式建模

## 风险5：图库承载过重
应对：
- 坚持多库存储
- 图库存语义骨架与索引，不存全文主数据

---

# 26. 结论

该系统不是简单的图数据库项目，也不是普通向量检索系统，而是一套面向网络通信领域的语义知识基础设施。  
其核心价值在于：

- 用本体稳定组织通信领域复杂知识
- 用知识加工流水线把公开网页语料转化为可计算知识
- 用多模存储承载文本、图关系和向量索引
- 用语义操作算子为上层应用提供统一能力
- 用受控演化机制保证系统能够持续吸收新知识而不发散

系统首版应坚持”先做小、做准、做可控”的原则，从单一高价值子域落地，再逐步扩展到全通信领域。

---

# 附录：当前实现状态（v0.2.0，2026-03-31）

以下记录已实际落地的技术实现方案，作为设计文档的补充。

## A1. 架构实现

### A1.1 分层架构落地

```
semcore 框架层（零外部依赖）
  ├── core/types.py         13 个领域数据类（OntologyNode, Fact, Segment, ...）
  ├── core/context.py       PipelineContext（强类型核心字段 + stage_outputs dict）
  ├── providers/base.py     5 个 Provider ABC（LLM, Embedding, Graph, Relational, Object）
  ├── ontology/base.py      OntologyProvider ABC（6 个抽象方法）
  ├── governance/base.py    3 个治理 ABC（ConfidenceScorer, ConflictDetector, EvolutionGate）
  ├── operators/base.py     SemanticOperator + OperatorMiddleware + OperatorRegistry
  ├── pipeline/base.py      Stage ABC + Pipeline（linear + branch + switch）
  └── app.py                SemanticApp + AppConfig（组合根）

src/ 领域实现层
  ├── providers/            5 个 Provider 实现（Postgres, Neo4j, Claude LLM, BGE-M3, MinIO）
  ├── ontology/             OntologyRegistry（单例缓存）+ YAMLOntologyProvider
  ├── governance/           TelecomConfidenceScorer / ConflictDetector / EvolutionGate
  ├── operators/            15 个 SemanticOperator 实现
  ├── pipeline/stages/      7 个 Stage 实现（ingest → segment → align → evolve → extract → dedup → index）
  ├── api/semantic/         9 个算子业务逻辑模块 + router.py
  ├── crawler/              Spider + ContentExtractor + DocumentNormalizer
  └── utils/                hashing / confidence / embedding / llm_extract / normalize / text / health / logging
```

**依赖方向严格遵守**：上层 → 下层接口，不直接访问实现。所有数据库操作通过 Provider 抽象（`app.store` / `app.graph`），不直接 `from src.db.postgres import`。

### A1.2 组合根（Composition Root）

`src/app_factory.py` 的 `build_app()` 统一装配所有组件：

```python
AppConfig(
    llm       = ClaudeLLMProvider(settings),
    embedding = BGEM3EmbeddingProvider(),
    graph     = Neo4jGraphStore(),
    store     = PostgresRelationalStore(),
    objects   = MinioObjectStore(settings),
    ontology  = YAMLOntologyProvider(registry),
    confidence_scorer = TelecomConfidenceScorer(),
    conflict_detector = TelecomConflictDetector(),
    evolution_gate    = TelecomEvolutionGate(),
    operators   = ALL_OPERATORS,   # 15 个算子
    middlewares = [TimingMiddleware(), LoggingMiddleware()],
    pipeline    = build_pipeline(),  # 7 阶段流水线
)
```

### A1.3 本地开发模式

`run_dev.py` 通过 `sys.modules` 补丁将 `src.db.postgres` 替换为 SQLite 内存实现，`src.db.neo4j_client` 替换为 dict 实现，无需 Docker 即可运行 `/lookup`、`/resolve` 等 API。

## A2. 7 阶段知识加工流水线

### A2.1 流水线总览

```
Pipeline()
  .add_stage(IngestStage())           # stage1: 采集入库
  .switch(key=doc_type, ...)          # stage2: 语义切分（按文档类型路由）
  .add_stage(AlignStage())            # stage3: 本体对齐
  .add_stage(EvolveStage())           # stage3b: 本体自动学习
  .add_stage(ExtractStage())          # stage4: 关系抽取
  .add_stage(DedupStage())            # stage5: 去重融合
  .add_stage(IndexStage())            # stage6: 图谱索引
```

### A2.2 Stage 1：Ingest（规则 C1-C5）

- **C3 内容去重**：SHA-256 content_hash，重复文档跳过（status='deduped'）
- **C4 文本提取**：trafilatura → readability-lxml → 正则降级；纯文本检测（.txt URL 或无 HTML 标签）跳过 HTML 提取器
- **C5 文档分类**：URL + 标题 + 内容模式匹配 → spec / vendor_doc / pdf / faq / tutorial / tech_article
- **存储**：原始 HTML → MinIO（content-addressed key: `raw/{sha256}.html`）；清洗文本 → MinIO（`cleaned/{norm_hash}.txt`，存入 `telecom-kb-cleaned` 桶）
- **纯文本保留**：`.txt` 格式文档（如 RFC）调用 `normalizer.normalize(text, preserve_newlines=True)` 保留行结构，不压缩换行

### A2.3 Stage 2：Segment（规则 S1-S4 + EDU/RST）

- **S1 结构化切分**：自动检测文档格式
  - Markdown 格式：按 `#` 标题层级切分
  - RFC 格式：按 `1.  Introduction`、`3.2  Terminology`、`ALLCAPS TITLE` 切分，跳过分页符 `\f`
  - 纯文本格式：按三空行或分页符切分
- **S2 语义角色分类**：12 个正则模式覆盖 definition / mechanism / constraint / config / fault / troubleshooting / best_practice / performance / comparison / table / code / ASCII diagram
- **S3 长度控制**：< 30 token 丢弃；> 1024 token 滑动窗口切片（window=512, overlap=64）
- **S4 置信度评估**：基于长度区间 / 语义角色识别 / 技术术语密度的启发式评分（0.70 ~ 0.95）
- **EDU/RST**：相邻 segment 对生成 RST 修辞关系（LLM 优先，规则降级，支持 11 种关系类型）

### A2.4 Stage 3：Align（规则 A1-A5）

- **A1 术语匹配**：
  - ≤ 3 字符别名使用 `\b` 词边界匹配（避免 “sp” 匹配 “specification”）
  - \> 3 字符别名使用子串匹配
  - 别名表来源：OntologyRegistry.alias_map（793+ 条别名，中英双语 + 厂商术语）
- **A2-A4 标签生成**：canonical / semantic_role / context 三类标签
- **A5 候选收集**：未匹配的大写术语 → `evolution_candidates` 表，使用 `normalized_form` 作为去重键，跨文档累积 `source_count` 和 `seen_source_doc_ids`

### A2.5 Stage 3b：Evolve（本体自动学习）

这是对设计方案第 15 节”受控演化机制”的完整实现：

- **评分**（仅对 source_count ≥ 2 的候选）：
  - source_diversity：不同站点覆盖度（distinct site_keys / 3.0）
  - temporal_stability：候选池驻留天数 / 14
  - structural_fit：与现有本体节点的 Jaccard 词汇重叠度
  - synonym_risk：与现有别名的子串重叠度
  - composite_score：加权综合分（权重来自 `evolution_policy.yaml`）
- **六项门控**（复用 `TelecomEvolutionGate.evaluate()`）：
  - min_source_count ≥ 3
  - min_source_diversity ≥ 0.6
  - min_temporal_stability ≥ 0.7
  - min_structural_fit ≥ 0.65
  - min_composite_score ≥ 0.65
  - synonym_risk ≤ 0.4
- **反漂移策略**：候选需在池中驻留 ≥ 7 天才参与门控
- **自动晋升**：composite_score ≥ 0.85 且有父节点 → 自动写入 Neo4j（`lifecycle_state='candidate'`，区别于人工审核的 `active` 节点）；低于阈值 → `pending_review` 等人工审核
- **YAML 不自动修改**：遵守”YAML 是 source of truth”原则，自动晋升只写 Neo4j

### A2.6 Stage 4：Extract（规则 R1-R4 + LLM）

- **规则模式**：15 个正则模板（uses_protocol / is_a / part_of / depends_on / requires / encapsulates / establishes / advertises / impacts / causes / implements / forwards_via / protects / monitored_by / configured_by）
- **LLM 抽取**：使用 OpenAI 兼容 API（支持 DeepSeek / 通义千问 / Claude），每个 segment 发送候选 node_id 列表 + 有效关系列表，返回 JSON 三元组数组
- **熔断器**：连续 3 次 LLM 失败 → 自动禁用 10 分钟，到期后自动尝试恢复
- **置信度评分**：`0.30×source_authority + 0.20×extraction_method + 0.20×ontology_fit + 0.20×cross_source + 0.10×temporal`

### A2.7 Stage 5：Dedup（规则 D1-D5）

- **段落去重**：64 位 SimHash（有符号，兼容 PG bigint）→ Hamming distance ≤ 3 → Jaccard ≥ 0.85 确认 → 标记 `lifecycle_state='superseded'`
- **事实去重**：exact (S,P,O) → 同一 merge_cluster_id
- **冲突检测**：same S+P, different O → `lifecycle_state='conflicted'` + conflict_records 记录

### A2.8 Stage 6：Index（规则 I1-I3 + Embedding）

- **置信度门控**：segment ≥ 0.5，fact ≥ 0.5
- **Neo4j 写入**：SourceDocument → KnowledgeSegment（带 `name` 属性用于图可视化）→ Fact → Evidence，含 BELONGS_TO / TAGGED_WITH / RELATED_TO / SUPPORTED_BY / EXTRACTED_FROM 关系
- **Embedding 写入**（如启用）：segment 文本 → BAAI/bge-m3 → pgvector 列；EDU 标题 + 内容双向量

## A3. 爬虫与反反爬

### A3.1 多策略 HTTP 客户端

```
fetch(url) 路由逻辑：
  hostname ∈ _TLS_FINGERPRINT_SITES → curl_cffi（Chrome 指纹模拟）
  hostname ∈ _SSL_SKIP_VERIFY_SITES → httpx(verify=False)
  默认 → httpx
  httpx 返回 403 → 自动降级 curl_cffi
  SSL 错误 → 自动降级 verify=False
```

- **User-Agent**：标准 Chrome UA
- **robots.txt 遵从**：per-site RobotFileParser 缓存
- **限速**：per-site rate_limit_rps 控制
- **内容寻址存储**：MinIO key = `raw/{sha256(content)}.html`，相同内容幂等，不同内容不覆盖

### A3.2 Worker 调度

- **种子 URL**：3 个数据源（RFC Editor 4 篇 / 3GPP 1 篇 / ITU-T 1 篇）
- **失败重试**：3 次渐进退避（5min → 30min → 120min）
- **空转退避**：前 3 次 INFO 级别 30s 间隔，之后 DEBUG 级别指数退避至 5 分钟上限

## A4. 本体工程

### A4.1 五层知识模型实例化

| 层 | YAML 文件 | 节点数 | 示例 |
|---|---|---|---|
| concept | ip_network.yaml | 74 | BGP, OSPF, TCP, UDP, IP, VLAN, MPLS... |
| mechanism | ip_network_mechanisms.yaml | 24 | PathVectorPropagation, SPFCalculation... |
| method | ip_network_methods.yaml | 22 | VRFProvisioningMethod, BGPPeeringMethod... |
| condition | ip_network_conditions.yaml | 20 | PreferVRFForIsolationRule, RouteLeakRisk... |
| scenario | ip_network_scenarios.yaml | 13 | MPLSL3VPNProvisioningScenario... |
| **合计** | | **153** | |

- **别名词典**：834 条（lexicon/aliases.yaml 156 条 + 各 domain YAML 内联别名）
- **受控关系类型**：54 种（top/relations.yaml）
- **新增核心节点（v0.2.0）**：TCP / UDP / IP / HTTP / TLS / SSH / Transport Layer / Application Layer

### A4.2 本体加载流程

```
YAML (ontology/) → scripts/load_ontology.py → Neo4j (OntologyNode + SUBCLASS_OF + Alias + ALIAS_OF)
                                             → PostgreSQL (lexicon_aliases 表)
                 → OntologyRegistry.from_default() → 内存单例（alias_map + nodes + relation_ids）
                 → YAMLOntologyProvider → 包装为 semcore OntologyProvider 接口
```

## A5. LLM 集成

### A5.1 多 API 兼容

`LLMExtractor` 自动检测 API 风格：
- `api.anthropic.com` → Anthropic Messages API
- `api.deepseek.com` / `dashscope.aliyuncs.com` / 其他含 `chat/completions` 的 → OpenAI 兼容 API
- **超时**：180 秒（大段文本抽取需要较长响应时间）
- **健康检查**：独立 15 秒短超时 ping，不阻塞启动

### A5.2 熔断器模式

```
连续失败计数 < 3 → 正常调用
连续失败 ≥ 3     → 禁用 LLM 600 秒（10 分钟）
冷却期结束       → 重置计数，允许重试
任何成功调用     → 立即重置计数
```

### A5.3 LLM 降级策略

- **启动**：LLM 不可用不阻止应用启动（PostgreSQL + Neo4j 才是必需）
- **Stage 2 RST**：LLM 不可用 → 规则映射表降级
- **Stage 2 标题**：LLM 不可用 → section_title → 首句截断
- **Stage 4 抽取**：LLM 不可用 → 仅规则模式（15 个正则模板）

## A6. 存储实现

### A6.1 PostgreSQL（13+ 张表）

核心表：source_registry / crawl_tasks / documents / segments / segment_tags / facts / evidence / conflict_records / extraction_jobs / ontology_versions / evolution_candidates / lexicon_aliases / t_edu_detail / t_rst_relation

### A6.2 Neo4j（运行时语义图）

节点类型：OntologyNode (153) / KnowledgeSegment / SourceDocument / Fact / Evidence / Alias (155+)

关系类型：SUBCLASS_OF (65) / ALIAS_OF (155+) / TAGGED_WITH / BELONGS_TO / RELATED_TO / SUPPORTED_BY / EXTRACTED_FROM

### A6.3 MinIO（对象存储）

- `telecom-kb-raw`：原始 HTML（content-addressed key）
- `telecom-kb-cleaned`：清洗后文本（normalized_hash key）

## A7. 已知限制与后续方向

1. **LLM 抽取速度**：每个 segment 独立调用 LLM（~90s/次），6 篇文档 292 个 segment 需要数小时。待优化：批量 segment 合并 prompt / 减少 candidate_node_ids 数量 / 跳过不必要的标题生成
2. **规则抽取召回率**：15 个正则模板对 RFC 自然语言覆盖有限，核心依赖 LLM
3. **Embedding 未启用**：BAAI/bge-m3 模型需手动下载，`EMBEDDING_ENABLED=false`
4. **向量检索算子**：`semantic_search` / `edu_search` 已实现但依赖 embedding 启用
5. **候选概念自动晋升**：需要跨多文档积累 + 7 天驻留期，首轮单批数据不会触发
6. **厂商文档采集**：种子 URL 仅含标准组织（RFC/3GPP/ITU），未接入 Cisco/Huawei 文档
