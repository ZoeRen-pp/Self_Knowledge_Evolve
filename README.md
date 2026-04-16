# 电信语义知识操作系统

> 面向网络集成交付工程师的治理驱动、持续演化、来源可溯的语义知识基础设施。

**不是 RAG，不是搜索引擎。** 核心价值：跨厂商术语归一化、配置依赖链分析、动网影响面评估、知识来源溯源与五维置信度评分、方案设计依据追溯、本体漂移防控、向量语义搜索。

## 系统能力

- **自动化知识采集**：内置 60+ 种子 URL（覆盖 RFC Editor、Huawei、Juniper、Arista、FRRouting、NetworkLessons 等 10 个站点），自动爬取、清洗、切分、对齐、抽取
- **五层本体模型**：概念 → 机制 → 方法 → 条件 → 场景，跨层推理
- **知识治理闭环**：候选词发现 → 五维评分 → 六道门控 → 人工审批 → YAML + Git 版本管理
- **质量自检**：5 维度 20+ 指标持续监控本体健康度
- **向量语义能力**：Embedding（三级回退：HTTP BGE-M3 服务 → Ollama → sentence-transformers）支持语义搜索、候选词去重、节点相似度检测、冲突发现
- **Dashboard**：5 Tab 可视化面板，支持离线导出为单文件 HTML

## 快速开始

### 本地开发（无需 Docker）

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run_dev.py                 # → http://127.0.0.1:8000/docs
```

### 生产环境

```bash
cp .env.example .env              # 编辑数据库连接等配置
psql -h localhost -U postgres -d telecom_kb -f scripts/init_postgres.sql
psql -h localhost -U postgres -d telecom_crawler -f scripts/init_crawler_postgres.sql
python scripts/init_neo4j.py
python scripts/load_ontology.py   # YAML → Neo4j + PG lexicon

# 启动 Worker（4线程：爬虫、Pipeline、监控、本体维护）
python worker.py

# 启动 API + Dashboard
uvicorn src.app:app --host 0.0.0.0 --port 8001
```

Dashboard：http://localhost:8001/dashboard

### 完全重置

```bash
python scripts/reset_and_run.py   # 杀进程→清数据→加载本体→启动Worker→启动API
```

### Embedding（三级回退）

```bash
# 方式一（推荐）：WSL2 BGE-M3 HTTP 服务（端口 :8000）
# .env: EMBEDDING_ENABLED=true  EMBEDDING_HTTP_URL=http://localhost:8000

# 方式二：Ollama
ollama pull bge-m3
# .env: EMBEDDING_ENABLED=true  OLLAMA_URL=http://localhost:11434

# 方式三：本地 sentence-transformers（自动回退）
# .env: EMBEDDING_ENABLED=true
```

### Docker（仅数据库）

```bash
docker-compose up -d              # PostgreSQL + Neo4j
```

## 架构总览

```
                    ┌─────────────────────────────────────┐
                    │           FastAPI (src/app.py)       │
                    │  21 语义算子 REST API + Dashboard    │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              │                    │                    │
    ┌─────────▼──────┐  ┌─────────▼──────┐  ┌─────────▼──────┐
    │  PostgreSQL     │  │    Neo4j       │  │    MinIO        │
    │  telecom_kb     │  │  图数据库      │  │  对象存储       │
    │  telecom_crawler│  │  5层本体+知识  │  │  原始/清洗文档  │
    └────────────────┘  └────────────────┘  └────────────────┘
              ▲                    ▲                    ▲
              │                    │                    │
    ┌─────────┴────────────────────┴────────────────────┴──────┐
    │                    Worker (4 线程)                        │
    │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐ │
    │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌─────────────┐ │
    │  │ Crawler  │ │ Pipeline │ │  Stats   │ │ Maintenance │ │
    │  │ 爬虫线程 │ │ 管线线程 │ │ 监控线程 │ │ 本体维护    │ │
    │  │ 60+种子  │ │ LLM硬依赖│ │ 5分钟周期│ │ 每日03:00   │ │
    │  └──────────┘ └──────────┘ └──────────┘ └─────────────┘ │
    └──────────────────────────────────────────────────────────┘
```

## Neo4j 图数据模型

Neo4j 中存在两个逻辑分离的层面：

```
本体推理层（用于图遍历：依赖闭包、影响传播、跨层推理）
────────────────────────────────────────────────────
  OntologyNode ──[DEPENDS_ON]──> OntologyNode
       │                              │
  [EXPLAINS]                     [IMPLEMENTED_BY]
       │                              │
  MechanismNode ──→ MethodNode ──→ ConditionRuleNode ──→ ScenarioPatternNode
       ▲
  Alias ──[:ALIAS_OF]──> (任意本体节点)

  本体边 = 多条 Fact 聚合后的结论（fact_count + max confidence）


知识溯源层（用于证据追溯：这个结论从哪来）
────────────────────────────────────────────────────
  Fact ──[:SUPPORTED_BY]──> Evidence ──[:EXTRACTED_FROM]──> KnowledgeSegment ──[:BELONGS_TO]──> SourceDocument

  Fact 通过属性引用（f.subject = node_id）桥接到本体节点，无图边
```

**为什么 Fact 不直接连接到 OntologyNode**：本体推理层只保留语义关系边（聚合结论），不被 Fact 生命周期（active/conflicted/superseded/merged）干扰。两层通过属性松耦合，推理查询和溯源查询各走各的路径，互不影响。

## 七阶段 Pipeline

```
Stage 1: Ingest    → 文本提取、降噪、质量门控、文档类型检测
Stage 2: Segment   → 三级切分（结构分割→语义角色→长度控制）、20种RST关系、discourse marker感知、滑动窗口512/64
Stage 3: Align     → 别名匹配 + Embedding模糊匹配、五层标签、LLM候选词发现（带分类）
Stage 3b: Evolve   → 五维评分、六道门控、自动晋升/待审核
Stage 4: Extract   → 5级优先抽取：P0 RST结构推断 → P1 单段LLM(≥3锚点+引用验证80%) → P2 合并上下文LLM(RST连续关系) → P2链 多跳RST链(3-4段) → P3 双节点共现兜底
Stage 5: Dedup     → SimHash + Embedding语义去重(cosine>0.90)、事实合并、本体驱动冲突检测(仅cardinality=one谓语触发D4)
Stage 6: Index     → 置信度门控、Neo4j写入（动态关系类型）、向量索引
```

## 五层本体模型

| 层 | YAML 文件 | Neo4j 标签 | 数量 | 定义 |
|----|-----------|------------|------|------|
| concept | `ip_network.yaml` | `OntologyNode` | 114 | YANG 模型参考的可配置对象（接口、协议实例、策略、VPN 等，可通过 CLI 操作） |
| mechanism | `ip_network_mechanisms.yaml` | `MechanismNode` | 24 | 协议算法与转发机制（如何工作） |
| method | `ip_network_methods.yaml` | `MethodNode` | 22 | 配置与排障流程（如何操作） |
| condition | `ip_network_conditions.yaml` | `ConditionRuleNode` | 20 | 适用条件、约束与决策规则（何时适用） |
| scenario | `ip_network_scenarios.yaml` | `ScenarioPatternNode` | 13 | 部署模式与业务场景（真实场景） |

关系：77 种（`ontology/top/relations.yaml`，支持 `cardinality: one` 标记函数型谓语）。别名：828 条（`ontology/lexicon/aliases.yaml`）。种子关系：187 条（axiom 85 + cross-layer 102）。

## 21 语义算子

| 算子 | 功能 |
|------|------|
| `lookup` | 术语查找 → 本体节点 |
| `resolve` | 模糊匹配解析 |
| `expand` | 节点展开（邻居、子类） |
| `path` | 两节点间路径 |
| `dependency_closure` | 依赖闭包 |
| `impact_propagate` | 故障影响传播 |
| `filter` | 条件过滤 |
| `evidence_rank` | 证据排序 |
| `conflict_detect` | 冲突检测（精确+Embedding语义） |
| `fact_merge` | 事实合并 |
| `candidate_discover` | 候选词发现 |
| `attach_score` | 置信度评分 |
| `evolution_gate` | 演化门控 |
| `context_assemble` | Agent上下文组装（五层推理链+完整段落） |
| `semantic_search` | 向量语义搜索 |
| `ontology_quality` | 本体质量评估（5维20+指标） |
| `stale_knowledge` | 过期知识检测 |
| `cross_layer_check` | 跨层连通性检查 |
| `graph_inspect` | 图结构检视 |
| `ontology_inspect` | 本体结构检视 |
| `edu_search` | 教育/学习资源搜索 |

## 知识治理

### 候选词生命周期

```
Pipeline发现 → discovered → Stage3b评分 → pending_review
                                              ↓
                         Maintenance(每24h) ─→ embedding去重 → LLM分类
                                              ↓            ↓        ↓
                                          new_concept   variant    noise
                                              ↓            ↓        ↓
                                         保留待审    合并知识到本体  删除
                                              ↓
                                    人工审批 → accepted → YAML + Git commit
```

### 置信度公式

```
score = 0.30×source_authority + 0.20×extraction_method
      + 0.20×ontology_fit + 0.20×cross_source_consistency + 0.10×temporal_validity
```

来源权威等级：S（IETF/3GPP/ITU-T/IEEE）→ 1.0 · A（Cisco/Huawei/Juniper）→ 0.85 · B（白皮书）→ 0.65 · C（博客论坛）→ 0.40

## 质量评估框架

| 维度 | 指标 | 说明 |
|------|------|------|
| G 粒度 | G1-G5 | Gini系数、超级节点比例、孤立节点、标签密度、万金油节点 |
| O 正交性 | O1-O5 | 谓语重叠、分布偏斜、集中度、利用率、节点语义相似度 |
| L 层间 | L1-L3 | 五层覆盖率、短路边、完整路径 |
| D 可发现性 | D1-D4 | 别名覆盖、关系利用、标签命中率 |
| S 结构 | S1-S5 | 连通性、依赖环、最短路径 |

## Embedding 能力

通过三级回退（HTTP BGE-M3 服务 → Ollama → sentence-transformers，均 1024 维）提供：

- **Stage 3 模糊匹配**：精确匹配失败时，embedding 匹配本体节点
- **Stage 5 语义去重**：相同 subject+object 的 facts，源文本 cosine > 0.90 → 合并
- **冲突检测**：embedding 发现语义相似但谓语矛盾的 facts
- **O5 节点相似度**：邻居Jaccard + 标签共现 + embedding余弦 三路信号
- **候选词去重**：周期性维护中 embedding 聚类合并重复候选词
- **同义词检测**：cosine > 0.90 直接判定，< 0.60 直接排除，中间才调 LLM

## 目录结构

```
├── src/
│   ├── api/semantic/          # 17 语义算子 API
│   ├── api/system/            # 系统管理 API（监控、审核）
│   ├── pipeline/stages/       # 7 阶段 Pipeline
│   ├── governance/            # 治理（冲突检测、演化门控、周期维护）
│   ├── stats/                 # 监控（采集器、调度器、质量计算）
│   ├── ontology/              # 本体注册表、验证器
│   ├── operators/             # 算子包装器
│   ├── providers/             # 存储提供者（PG、Neo4j、MinIO、LLM）
│   ├── crawler/               # 爬虫
│   ├── utils/                 # 工具（embedding、LLM、归一化、哈希）
│   ├── dev/                   # 开发模式（内存数据库）
│   └── config/                # 配置
├── semcore/                   # 零依赖框架抽象层（可独立发布）
├── ontology/                  # 本体 YAML（版本控制的真相来源）
│   ├── domains/               # 五层节点定义
│   ├── top/                   # 关系类型定义
│   ├── lexicon/               # 别名词典
│   ├── patterns/              # 外部化正则模式
│   ├── seeds/                 # 种子关系
│   └── governance/            # 演化策略
├── static/                    # Dashboard HTML
├── scripts/                   # 运维脚本
├── worker.py                  # Worker（4线程）
└── docs/                      # 设计文档
```

## 配置

关键环境变量（`.env`）：

| 变量 | 说明 | 默认 |
|------|------|------|
| `LLM_ENABLED` | 启用 LLM（Stage 4 抽取、候选词发现） | `false` |
| `LLM_API_KEY` | LLM API Key | - |
| `EMBEDDING_ENABLED` | 启用 Embedding | `false` |
| `EMBEDDING_HTTP_URL` | HTTP BGE-M3 服务地址（优先） | `http://localhost:8000` |
| `OLLAMA_URL` | Ollama 地址（次选） | `http://localhost:11434` |
| `OLLAMA_EMBED_MODEL` | Ollama Embedding 模型 | `bge-m3` |
| `ONTOLOGY_MAINTENANCE_INTERVAL_HOURS` | 本体维护周期（小时） | `24` |

## 设计文档

- `docs/architecture-design-20260406.md` — 系统架构设计（最新）
- `docs/development-spec-20260406.md` — 开发规格说明（最新）
- `docs/product-architecture-4plus1-20260413.md` — 4+1 产品架构视图
- `docs/candidate-dedup-design.md` — 候选词去重设计
- `docs/embedding-enhancements-design.md` — Embedding 增强设计
- `docs/telecom-ontology-design.md` — 五层本体模型设计
- `docs/semcore-framework-design.md` — semcore 框架设计