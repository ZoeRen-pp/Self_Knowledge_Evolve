# 系统架构设计

日期：2026-04-06
版本：v0.4
状态：当前实现

## 1. 系统定位

电信语义知识操作系统 — 面向网络/电信领域的治理驱动、持续演化、来源可溯的知识基础设施。

**不是**：RAG 系统、搜索引擎、知识图谱可视化工具。
**是**：跨厂商术语归一化平台、结构化知识抽取与治理引擎、本体驱动的语义操作系统。

## 2. 分层架构

```
┌─────────────────────────────────────────────────────────────────────┐
│  表现层  │  FastAPI REST API + Dashboard (static/dashboard.html)    │
├─────────────────────────────────────────────────────────────────────┤
│  算子层  │  21 个语义算子 (src/operators/)                          │
│          │  OperatorRegistry 统一调度                               │
├─────────────────────────────────────────────────────────────────────┤
│  治理层  │  src/governance/                                         │
│          │  ├── confidence_scorer.py    置信度评分                  │
│          │  ├── conflict_detector.py    冲突检测（精确+Embedding）  │
│          │  ├── evolution_gate.py       六道演化门控               │
│          │  └── maintenance.py          周期性本体维护             │
├─────────────────────────────────────────────────────────────────────┤
│  管线层  │  7 阶段 Pipeline (src/pipeline/stages/)                  │
│          │  Ingest → Segment → Align → Evolve → Extract → Dedup    │
│          │  → Index                                                 │
├─────────────────────────────────────────────────────────────────────┤
│  本体层  │  src/ontology/registry.py   内存注册表                   │
│          │  ontology/*.yaml            YAML 真相来源               │
│          │  五层模型: concept→mechanism→method→condition→scenario   │
├─────────────────────────────────────────────────────────────────────┤
│  存储层  │  PostgreSQL (telecom_kb + telecom_crawler)               │
│          │  Neo4j (图数据库)                                        │
│          │  MinIO (对象存储)                                        │
│          │  Ollama (Embedding 推理)                                 │
├─────────────────────────────────────────────────────────────────────┤
│  框架层  │  semcore/ (零依赖抽象框架，可独立发布)                   │
│          │  ABCs: Stage, Pipeline, SemanticOperator, GraphStore,    │
│          │        RelationalStore, ObjectStore, ConflictDetector    │
└─────────────────────────────────────────────────────────────────────┘
```

## 3. 进程模型

### 3.1 Worker 进程（worker.py）

4 个 daemon 线程，通过 `threading.Event` 协调优雅退出：

| 线程 | 职责 | 调度 | 依赖 |
|------|------|------|------|
| `crawler` | 爬取 pending 任务，存入 MinIO | 持续，idle 时指数退避 | spider.py, crawler_store |
| `pipeline` | 处理 raw 文档，走 7 阶段 pipeline | 持续，等待新 raw 文档 | pipeline/, store, graph |
| `stats` | 采集系统统计快照 | 每 5 分钟 | collector.py, scheduler.py |
| `maintenance` | 本体演化维护（embedding去重+LLM分类+清理） | 每 24 小时 | governance/maintenance.py |

线程之间**无直接通信**，通过数据库状态间接协调：
- crawler 写 `documents(status='raw')` → pipeline 读
- pipeline 写 `evolution_candidates` → maintenance 读
- maintenance 写 `segment_tags`, `lexicon_aliases` → pipeline 下次读

### 3.2 API 进程（src/app.py）

FastAPI 单进程，无状态，只做查询和写操作路由：
- `/api/v1/semantic/` — 21 个语义算子
- `/api/v1/system/` — 监控、审核、钻取
- `/static/dashboard.html` — 前端面板
- `/docs` — OpenAPI 文档

## 4. 数据库架构

### 4.1 PostgreSQL — telecom_kb（知识库）

```
公共表:
  documents          文档元数据 + 状态机 (raw→cleaning→segmented→indexed)
  segments           文本段落 (三级切分结果)
  segment_tags       段落标签 (canonical/semantic_role/context/mechanism_tag/...)
  t_rst_relation     RST 篇章关系 (21种)
  facts              知识条目 (S-P-O 三元组 + 置信度)
  evidence           证据链 (fact → segment → document 溯源)
  lexicon_aliases    别名词典
  system_stats_snapshots  监控快照

governance schema:
  evolution_candidates    候选词池 (含五维评分)
  conflict_records        冲突记录
  review_records          审核记录
  ontology_versions       本体版本记录
```

### 4.2 PostgreSQL — telecom_crawler（爬虫库，独立部署）

```
  source_registry    站点注册 (site_key, source_rank, rate_limit)
  crawl_tasks        爬取任务队列
  extraction_jobs    抽取任务队列
```

### 4.3 Neo4j（图数据库）

```
节点类型:
  OntologyNode (100)       概念层
  MechanismNode (24)       机制层
  MethodNode (22)          方法层
  ConditionRuleNode (20)   条件层
  ScenarioPatternNode (13) 场景层
  Alias                    别名节点
  Fact                     知识条目节点

关系类型:
  SUBCLASS_OF              层级关系
  ALIAS_OF                 别名关系
  DEPENDS_ON, USES_PROTOCOL, ESTABLISHES, ...  71种动态关系
  MERGE by (src, type, dst), fact_count 计数
```

### 4.4 MinIO（对象存储）

```
  telecom-kb-raw/      原始 HTML 文件
  telecom-kb-cleaned/  清洗后文本
```

### 4.5 Ollama（Embedding 推理）

```
  模型: bge-m3 (1024维, 中英双语)
  API: POST http://localhost:11434/api/embed
  用途: 语义搜索、候选词去重、模糊匹配、冲突检测、节点相似度
```

## 5. Pipeline 详细设计

### Stage 1: Ingest（清洗入库）

输入：`source_doc_id`（documents 表中 status='raw' 的记录）
流程：从 MinIO 加载 → HTML 提取 → 降噪 → 质量门控 → 写回 cleaned/ → 更新状态
输出：清洗后文本存入 MinIO cleaned/，documents 状态更新

### Stage 2: Segment（三级切分）

输入：清洗后文本
流程：
1. 段落级切分（双换行 / heading 分界）
2. 句子级切分（spaCy 或规则）
3. 长段落滑动窗口（token_count > 阈值时）
4. RST 篇章关系抽取（21 种，6 类）
5. 语义角色分类（从外部 YAML 模式匹配）
输出：segments 表 + t_rst_relation 表

### Stage 3: Align（本体对齐）

输入：segments
流程：
1. 精确别名匹配（`_find_terms`，word-boundary 感知）
2. Embedding 模糊匹配（精确匹配 0 命中时，cosine > 0.80）
3. 五层标签写入（canonical / mechanism_tag / method_tag / condition_tag / scenario_tag）
4. 候选词发现（LLM 分类：new_concept / variant / noise）
5. 停用词过滤 + Embedding 去重
输出：segment_tags 表 + evolution_candidates 表

### Stage 3b: Evolve（轻量评分）

输入：当前文档关联的候选词
流程：五维评分（source_diversity, temporal_stability, structural_fit, synonym_risk, source_authority）→ 六道门控 → 状态流转
输出：candidates 更新评分和状态

### Stage 4: Extract（关系抽取）

输入：segments + segment_tags
流程：
1. LLM 优先：发送 segment 文本 + 已标记节点，要求返回 (S, P, O) 三元组
2. 合并上下文重试：单段无结果时合并相邻段落重试
3. 共现兜底：恰好 2 个标记节点 → 1 条低置信度共现关系
4. LLM 可提议新谓语 → 进入 candidates(type='relation')
输出：facts 表 + evidence 表

### Stage 5: Dedup（去重）

输入：当前文档的 facts
流程：
1. 段落级 SimHash + Jaccard 去重
2. 精确 (S, P, O) 匹配 → 多源合并（保留最高置信度）
3. Embedding 语义去重（同 S+O，源文本 cosine > 0.90 → 合并）
4. 冲突检测（同 S+P 不同 O → 标记冲突）
输出：facts 状态更新 + conflict_records

### Stage 6: Index（入库）

输入：active facts
流程：
1. 置信度门控（< 阈值不入图）
2. Neo4j MERGE by (src, type, dst)，动态关系类型（predicate → UPPER_SNAKE）
3. fact_count 累加
输出：Neo4j 关系写入

## 6. 治理架构

### 6.1 候选词三层过滤

```
Pipeline (Stage 3)     粗筛：LLM 分类 + 停用词 + Embedding
    ↓
Pipeline (Stage 3b)    打分：五维评分 + 六道门控
    ↓
Maintenance (每24h)    精筛：Embedding 聚类去重 + LLM 批量分类
    ↓                         ↓ noise → 删除
    ↓                         ↓ variant → 合并知识到原有节点 + 补别名
    ↓                         ↓ new_concept → 保留待审
    ↓
Human (Review API)     终审：approve → YAML + Neo4j + Git commit
                              reject → 标记
                              merge → 合并多个候选为一个
```

### 6.2 Variant 知识合并

当 maintenance 判定候选词为已有本体节点的变体时：
1. 找到匹配的本体节点（embedding 或 LLM 返回的 parent_concept）
2. 候选词 examples 中的 segment_ids → 补挂该节点的 canonical tag
3. 候选词 surface_forms → 追加为该节点的别名（lexicon_aliases + alias_map）
4. 候选词标记为 `variant_merged`

知识不丢失，归位到正确节点。

### 6.3 审批入库

approve 操作写入：
1. Neo4j：MERGE OntologyNode + SUBCLASS_OF 父节点 + Alias 节点
2. PG：lexicon_aliases
3. YAML：`ontology/domains/ip_network_evolved.yaml` + `ontology/lexicon/aliases.yaml`
4. Git：自动 commit（版本管理）
5. BackfillWorker：回填已有 segments 的标签

## 7. Embedding 架构

### 7.1 Backend 选择（自动检测）

```
src/utils/embedding.py
  ├── 优先: Ollama API (http://localhost:11434/api/embed)
  │         无 Python 依赖，推理快，模型由 Ollama 管理
  └── 兜底: sentence-transformers (BAAI/bge-m3)
            需要 pip install，首次加载慢，GPU 可加速
```

### 7.2 使用场景

| 场景 | 文件 | 逻辑 |
|------|------|------|
| Stage 3 模糊匹配 | stage3_align.py | 精确匹配 0 命中 → embedding vs 本体节点, >0.80 |
| Stage 5 语义去重 | stage5_dedup.py | 同 S+O 的 facts, 源文本 cosine >0.90 → 合并 |
| 冲突检测 | conflict_detector.py | S+O embedding 相似但 predicate 不同 |
| O5 节点相似度 | ontology_quality.py | 三路信号: 邻居Jaccard + 标签共现 + embedding |
| 候选词去重 | maintenance.py | 批量聚类, cosine >0.85 → 合并 |
| 同义词检测 | review.py | >0.90 直接判定, <0.60 直接排除, 中间调 LLM |

## 8. 监控与质量

### 8.1 Stats 采集（每 5 分钟）

StatsCollector 采集 7 类指标 → JSON 快照存入 system_stats_snapshots：
- knowledge: 文档/段落/知识条目/证据/RST关系/Neo4j 统计
- quality: 覆盖率/置信度/冲突率/弱证据率
- graph_health: 孤立节点/度分布/谓语使用
- ontology_health: 继承深度/分支因子/别名覆盖
- pipeline: 积压/失败/吞吐
- evolution: 候选词分布
- sources: 来源权威等级/抽取方法分布

### 8.2 本体质量评估（5 维度 20+ 指标）

OntologyQualityCalculator 独立计算：
- G: 粒度合理性（Gini, 超级节点, 孤立节点, 标签密度, 万金油节点）
- O: 正交性（谓语重叠, 分布偏斜, 集中度, 利用率, 节点语义相似度）
- L: 层间连通（五层覆盖率, 短路边, 完整路径）
- D: 可发现性（别名覆盖, 关系利用, 标签命中率）
- S: 结构健康（连通性, 依赖环, 最短路径）

每个指标可钻取到具体数据（21 个 drilldown 端点）。

### 8.3 Dashboard（5 Tab）

| Tab | 功能 |
|-----|------|
| 系统总览 | 流水线流量图 + 来源分布 + 最近文档 |
| 知识探索 | 搜索术语 → 节点信息 + facts + 五层推理链 + 源文本 |
| 质量评估 | 5 维度雷达图 + 每个指标可点击查看详情 |
| 知识演化 | 候选词分布 + 审核时间线 + approve/reject/merge |
| 运行监控 | 运行状态 + 趋势图 |

支持导出为离线 HTML（`scripts/export_dashboard.py`）。

## 9. 外部化配置

所有正则模式外部化到 YAML，本体变化不改代码：

```
ontology/patterns/
  ├── semantic_roles.yaml        22 种语义角色匹配模式
  ├── context_signals.yaml       6 种上下文信号
  ├── predicate_signals.yaml     13 种谓语信号
  └── candidate_stopwords.yaml   候选词停用词表
ontology/seeds/
  ├── cross_layer_relations.yaml 56 条跨层种子关系
  ├── axiom_relations.yaml       48 条公理关系
  └── classification_fixes.yaml  3 条分类修正
ontology/governance/
  └── evolution_policy.yaml      演化策略（评分权重、门控阈值）
```