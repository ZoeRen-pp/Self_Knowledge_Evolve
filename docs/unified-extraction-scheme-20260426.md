# 概念层抽取统一方案 — 衔接去年 3 个 workflow 与今年语义基础设施
**日期：2026-04-26 | 版本：0.1（草案）**

---

## 1. 背景与目标

### 1.1 去年沉淀的 3 个 workflow

| 代号 | 输入 | 抽取方法 | 输出 | 特点 |
|------|------|----------|------|------|
| **W1** | 清洗后的 IP 命令行 + 视图关系数据 | LLM 抽取 + 大量校验 | concept 实体 + 关系 + CLI→对象解析规则 | 通用 IP 域，依赖 LLM 但有重校验 |
| **W2** | 云核心网产品手册 | 工具链文本解析 | 手册中指定章节的内容/对象 | 文档型输入，主要做内容定位与抽取 |
| **W3** | 清洗后的云核心网 CLI（含参数引用关系） | 确定性算法 | concept 实体 + 关系 | 云核 CLI 高度规范，可不用 LLM |

三者各自独立，输入/方法/输出形态不同，**但本质都是"专用源 → 概念层实体与关系"**。

### 1.2 今年在做的语义知识基础设施

5 层本体（concept / mechanism / method / condition / scenario）+ 7-stage 流水线（Ingest / Segment / Align / Evolve / Extract / Dedup / Index）+ 治理 + 5-原语声明式查询引擎 + Copilot。**通用、自动、广覆盖**。

### 1.3 本方案要解决的问题

1. **统一 3 个 workflow 的实现** — 抽出共同抽象，沉淀给产品化团队，避免每接一个新 vendor / 新场景都重写
2. **承接今年的语义基础设施** — 让明年产品化团队接手时，3 个 workflow 的产物能直接喂入今年构建的本体/治理/查询层，而不是平行系统

---

## 2. 共同抽象的发现

去年 3 个 workflow 的差异只在 **输入源** 和 **抽取方法的置信度**，输出形态完全可以归并为同一种结构：

```
ConceptNode + relations + (optional) parse_rules
```

而抽取过程都可以拆成 4 个职责明确的阶段：

```
[源] → [SourceAdapter] → [Common IR] → [Extractor] → [ExtractionResult] → [ValidationChain] → [Concept Layer]
```

不同 workflow 的差异落在不同的阶段：
- **W1**：CLI/视图 SourceAdapter + LLMExtractor + 多个 Validator
- **W2**：手册 SourceAdapter + LLMExtractor（带 section anchor）
- **W3**：CLI SourceAdapter + DeterministicExtractor

**关键观察**：3 类 Extractor 与 N 个 SourceAdapter / N 个 Validator 是**正交的**，可以自由组合。这给了插件化架构的天然依据。

---

## 3. 统一架构

### 3.1 总图

```
┌──────────────────────────────────────────────────────────────────────────┐
│  输入源（异构、可扩展）                                                     │
│  IP CLI+视图(W1)  │  云核手册(W2)  │  云核 CLI(W3)  │  ...新 vendor       │
└──────────┬─────────────┬───────────────┬────────────────┬────────────────┘
           │             │               │                │
       SourceAdapter  SourceAdapter  SourceAdapter   SourceAdapter
           │             │               │                │
           ▼             ▼               ▼                ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Common IR（标准化中间表示）                                                │
│   Segment {                                                                │
│     id, source_metadata, section_path, raw_text,                          │
│     parameters[], references[], extracted_at                              │
│   }                                                                        │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
            ┌────────────────┼────────────────┐
            ▼                ▼                ▼
   DeterministicExtractor  LLMExtractor   HybridExtractor
   （W3 类）                 （W1, W2 类）   （rule + LLM 兜底）
            │                │                │
            └────────────────┼────────────────┘
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  ExtractionResult（统一产物 schema）                                       │
│  - entities:    [ConceptNode（id, parent_class, attrs, source_ref）]      │
│  - relations:   [(subject, predicate, object, evidence)]                  │
│  - parse_rules: [(cli_pattern → object_template)]   // 可选               │
│  - confidence:  S | A | B | C                                              │
└────────────────────────────┬─────────────────────────────────────────────┘
                             │
                  ValidationChain（可插拔流水线）
                  ├─ schema_check       (字段齐全)
                  ├─ ontology_fit       (实体在本体定义内)
                  ├─ ref_integrity      (引用对象都存在)
                  ├─ cross_source       (多源一致性)
                  └─ llm_self_verify    (语义合理性)
                             │
                             ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  Concept Layer（统一本体 — 与今年语义基础设施共享）                          │
│  IP.BGP_INSTANCE / IP.OSPF_INTERFACE / IP.VRF_INSTANCE / ...               │
└──────────────────────────────────────────────────────────────────────────┘
```

### 3.2 核心抽象（4 个 ABC）

```python
class SourceAdapter(ABC):
    """把异构输入源标准化成 Common IR。"""
    @abstractmethod
    def parse(self, raw_input: bytes | str | Path) -> list[Segment]: ...
    @abstractmethod
    def source_type(self) -> str: ...   # "ip_cli" / "cloud_core_manual" / ...

class Extractor(ABC):
    """从 Common IR 抽出实体与关系。"""
    @abstractmethod
    def extract(self, segments: list[Segment]) -> ExtractionResult: ...
    @abstractmethod
    def extractor_kind(self) -> str:    # "deterministic" / "llm" / "hybrid"
        ...
    @abstractmethod
    def default_confidence(self) -> str:  # "S" / "A" / "B" / "C"
        ...

class Validator(ABC):
    """对 ExtractionResult 做单一职责校验。"""
    @abstractmethod
    def validate(self, result: ExtractionResult, ctx: ValidationContext) -> ValidationReport: ...

class Sink(ABC):
    """把验证后的结果写入下游（本体库、文件、API）。"""
    @abstractmethod
    def write(self, result: ExtractionResult, report: ValidationReport) -> WriteReceipt: ...
```

### 3.3 数据契约（统一 Schema）

```python
@dataclass
class Segment:
    id: str
    source_type: str           # "ip_cli" 等
    source_metadata: dict      # vendor / version / file / line
    section_path: list[str]    # 章节定位（手册类）或 view path（CLI 类）
    raw_text: str
    parameters: dict           # CLI 类源解析出的命令参数
    references: list[str]      # 该段落引用的其它对象/参数

@dataclass
class ConceptNode:
    node_id: str               # IP.BGP_INSTANCE 等（与今年本体共享 ID 空间）
    parent_class: str          # 上层节点
    attributes: dict
    source_ref: SourceRef      # 回指到产生它的 Segment

@dataclass
class Relation:
    subject: str
    predicate: str             # 复用 ontology/top/relations.yaml
    object: str
    evidence: SourceRef

@dataclass
class ParseRule:
    """可选：CLI pattern → 对象模板，供配置生成、变更影响分析复用。"""
    vendor: str
    cli_pattern: str           # 正则或模板
    object_template: dict      # 生成的目标对象骨架

@dataclass
class ExtractionResult:
    entities: list[ConceptNode]
    relations: list[Relation]
    parse_rules: list[ParseRule]   # 可空
    confidence: str                # S/A/B/C
```

### 3.4 三个 workflow 的 retrofit 映射

| 阶段 | W1（IP CLI+视图） | W2（云核手册） | W3（云核 CLI 算法） |
|------|------------------|----------------|--------------------|
| SourceAdapter | `IpCliAdapter` + `ViewRelationLoader` | `CloudCoreManualAdapter` | `CloudCoreCliAdapter` |
| Extractor | `LLMExtractor`（保留现有 prompt） | `LLMExtractor`（+ section anchor） | `DeterministicCliExtractor` |
| 默认 confidence | A | B | S |
| Validator 链 | schema + ontology_fit + cross_source + llm_self_verify | schema + ref_integrity | schema_check（其他可省） |
| 是否产 parse_rules | 是 | 否 | 是 |
| Sink | OntologySink（写本体） + ParseRuleSink（写规则库） | OntologySink | OntologySink + ParseRuleSink |

---

## 4. 与今年语义知识基础设施的承接

### 4.1 关系定位

| 维度 | 去年 3 workflow（产品化） | 今年语义基础设施（技术预研） |
|------|--------------------------|----------------------------|
| 输入源 | **垂直专精**（特定 vendor / 产品） | **通用**（RFC / 标准 / blog / vendor 手册） |
| 抽取方法 | **高置信**（确定算法或重校验 LLM） | **广覆盖**（LLM + RST + embedding） |
| 输出层 | **只到 concept** | **5 层（concept / mech / method / cond / scene）** |
| 治理 | 各自处理 | **统一 governance + evolution** |
| 查询出口 | 无统一入口 | **5 原语查询 + Copilot** |

二者**互补**：去年 workflow 给 S/A 级权威知识，今年基础设施给广覆盖与高层抽象。

### 4.2 集成点

```
┌── 去年 W1/W2/W3（统一为本方案） ──┐         ┌── 今年语义基础设施 ──┐
│                                   │         │                      │
│  专用源专用抽取                    │ 注入 →  │  Stage 1-6 流水线     │
│  → 高置信 ConceptNode + Relations │         │  - documents 表        │
│  → ParseRules                     │         │  - segments 表         │
│                                   │         │  - facts 表            │
└───────────────────────────────────┘         │  - ontology YAML       │
                                              │  - governance schema   │
                                              │  - QueryEngine        │
                                              │  - Copilot            │
                                              └──────────────────────────┘
                                                         ↓
                                                工程师工具 / API
```

### 4.3 5 个具体桥接点

1. **Concept 节点 ID 空间统一**：去年 W1/W3 抽出的对象直接落到 `ontology/domains/ip_network.yaml` 同一 IP.* schema —— 现状已经一致，不需改造
2. **关系映射**：去年的"包含 / 依赖 / 属于"映射到现有 `part_of` / `depends_on` / `belongs_to_domain` 等本体定义谓词
3. **Parse Rules 入新表**：建 `concept_parse_rules` 表（vendor, cli_pattern, object_template, source_workflow），支持配置生成与反向变更影响分析
4. **置信度分层利用**：S 级（W3 确定性）/ A 级（W1 LLM+校验）/ B 级（W2 手册）一并写入 `confidence` 字段，governance 自动按等级处理冲突
5. **Source rank 已支持**：现有 confidence formula（`0.30×source_authority + 0.20×extraction_method + ...`）已有 S/A/B/C 等级，去年 workflow 直接复用

### 4.4 跨年知识衔接示意

```
W3 算法（云核 CLI）
   ↓ 写入
Concept: IP.AMF_INSTANCE { confidence: S }
   ↓
今年 Stage 4 LLM 从云核手册抽出
   ↓
Mechanism: MECH.SBI_AUTH_HANDSHAKE { confidence: A, derived_from: amf_doc }
   ↓
Stage 5 D4 一致性检查 → 自动建立跨层关联
   ↓
QueryEngine: 工程师问"AMF 鉴权失败该查哪些配置？"
   → seed(IP.AMF_INSTANCE) → expand(part_of) → expand(participates_in MECH.SBI_AUTH_HANDSHAKE)
   → project(troubleshooting steps)
```

---

## 5. 给产品化团队的扩展模型

明年产品化团队接手后，扩展新 vendor / 新场景只动 3 个明确的扩展点，**不碰**框架：

### 5.1 三个扩展点

1. **加新 SourceAdapter** —— 解析新 vendor 的 CLI / manuals
2. **加新 Extractor** —— 针对该 vendor 的专用算法（W3 模式扩展）或 LLM prompt 调优
3. **加新 Validator** —— 针对该 vendor 的合规 / 质量规则

### 5.2 不动的稳定层

- 本体 schema（IP.* 节点定义、relations.yaml）
- ValidationChain 框架
- QueryEngine 与 5 原语
- Copilot 与 LLM 编排
- Governance 流程

### 5.3 接入新 vendor 的 5 步指南

1. 实现 `SourceAdapter` —— 输入 vendor 原始格式，输出 `Segment[]`
2. 选择或实现 `Extractor` —— 优先复用 `LLMExtractor`，规范度高的可写 `DeterministicExtractor`
3. 在本体 `ontology/domains/` 加 vendor 节点（如有新对象类型）
4. 配置 `ValidationChain`（一般用默认链 + 1-2 个 vendor 特有 validator）
5. 注册到 `ExtractionPipeline` 工厂，CI 跑端到端 PoC

预期工作量：**1 个新 vendor 接入 ≈ 1-2 周**（不含 prompt 调优迭代）。

---

## 6. 落地路径

### Phase 1：抽象层落地（1-2 周）
- 在 `semcore/extraction/` 下定义 4 个 ABC + 数据契约 dataclass
- 写 1 个参考实现：`ReferenceLLMExtractor`

### Phase 2：去年 W3 retrofit 验证（1 周）
- 把云核 CLI 算法包成 `CloudCoreCliAdapter` + `DeterministicCliExtractor`
- 跑一遍历史样本，对比新旧输出一致性

### Phase 3：与今年基础设施集成 PoC（1 周）
- 选 1 个云核场景，跑通"W3 → Concept Layer → QueryEngine → Copilot"全链路
- 验证 confidence 分层、跨层关联、查询返回正确

### Phase 4：W1/W2 retrofit（2-3 周）
- W1 的 LLM prompt 包成 `LLMExtractor` 子类
- W2 的工具链包成 `CloudCoreManualAdapter` + `LLMExtractor` 配置
- 三个 workflow 同接口，并入主流程

### Phase 5：交付产品化团队（2 周）
- 写产品化扩展指南文档（基于本文档）
- 准备 1 个新 vendor 的样例接入 demo
- 培训交接

**总计 ~7-9 周。**

---

## 7. 沉淀给产品化团队的产出

1. **架构总图与设计文档**（本文档 + C4 风格扩展）
2. **接口契约 + 示例代码**（4 个 ABC + 1 个端到端示例）
3. **3 workflow 的 retrofit 报告**（W1/W2/W3 映射 + 工作量评估）
4. **新 vendor 接入指南**（5 步 step-by-step）
5. **集成 PoC 仓库**（1 个真实 vendor 全链路示例）

---

## 8. 待决事项

1. **W2 是否一定走 LLM**：手册结构如果足够规范，部分内容可考虑 deterministic 抽取
2. **ParseRules 与 ontology 同步策略**：parse_rule 改了之后，是否要重抽历史 segments？
3. **Vendor 私有节点入主本体的审批流程**：跑 governance 还是 vendor namespace 隔离？
4. **W1 的视图关系数据**：是单独存还是和 segment.references 合并？需要看实际数据形态决定

---

## 附录 A：与现有文档的关系

- `docs/architecture-design-20260406.md` — 今年语义基础设施总架构（本方案承接其 concept 层接口）
- `docs/telecom-ontology-design.md` — 5 层本体设计（本方案的 Concept Layer 与之共用）
- `docs/query-engine-design-20260417.md` — 5 原语查询代数（产品化扩展不涉及）
- `docs/semcore-framework-design.md` — semcore 框架抽象（4 个新 ABC 应放在 `semcore/extraction/`）

## 附录 B：术语对照

| 本方案术语 | 今年基础设施同义词 | 去年 workflow 对应 |
|-----------|------------------|-------------------|
| SourceAdapter | Stage 1 (Ingest) 的源解析 | 各 workflow 的输入预处理 |
| Common IR (Segment) | Stage 2 输出的 Segment | W1/W3 的 CLI 中间表示 |
| Extractor | Stage 4 (Extract) | LLM 抽取/确定性算法 |
| ValidationChain | Stage 5 (Dedup) + governance | W1 的校验步骤 |
| Concept Layer | ontology IP.* nodes | concept 层对象 |
