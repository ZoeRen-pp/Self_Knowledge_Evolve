# 正则模式外部化 — 设计方案

**日期**：2026-04-04
**状态**：方案已确认，待开发

---

# 一、问题

Pipeline 中 4 组正则模式硬编码在 Python 代码里：

| 位置 | 变量 | 数量 | 用途 |
|------|------|------|------|
| Stage 2 `stage2_segment.py` | `_ROLE_PATTERNS` | 12 | 语义角色分类（definition/mechanism/config...） |
| Stage 3 `stage3_align.py` | `_CONTEXT_PATTERNS` | 6 | 上下文场景检测（数据中心/园区/5GC...） |
| Stage 4 `stage4_extract.py` | `RELATION_PATTERNS` | 15 | 关系抽取（uses_protocol/depends_on...） |
| Stage 4 `stage4_extract.py` | `_PREDICATE_SIGNALS` | 13 | 谓语信号检测（共现策略用） |

**问题**：本体会演化（新增概念/关系/场景），但这些正则不会自动更新。每次本体变更都需要改 Python 代码，违反"本体定义与代码分离"原则。

---

# 二、方案

## 2.1 新增 YAML 配置目录

```
ontology/
├── patterns/
│   ├── semantic_roles.yaml      # Stage 2 语义角色分类
│   ├── context_signals.yaml     # Stage 3 上下文场景检测
│   ├── relation_extraction.yaml # Stage 4 关系抽取正则
│   └── predicate_signals.yaml   # Stage 4 谓语信号正则
```

## 2.2 YAML 格式

### semantic_roles.yaml

```yaml
# 语义角色分类：正则匹配 → segment_type
# 按优先级从高到低排列，首个命中即返回
roles:
  - type: definition
    patterns:
      - '\b(is defined as|refers to|is a type of|means that|definition of)\b'
      - '\b(header format|field.{0,20}(?:contain|indicate|specif))\b'
      - '\b(data unit|segment format|datagram format|packet format)\b'

  - type: mechanism
    patterns:
      - '\b(works by|mechanism|algorithm|process of|how it)\b'
      - '\b(state machine|handshake|retransmit|acknowledgment|three.way)\b'
      - '\b(sliding window|flow control|congestion|encapsulat|multiplexing)\b'

  - type: constraint
    patterns:
      - '\b(must|shall|required|mandatory|limitation|constraint|not allowed)\b'

  # ... 以此类推
```

### context_signals.yaml

```yaml
# 上下文场景检测：正则匹配 → 场景标签
signals:
  - label: 数据中心
    patterns:
      - '\bdata center\b|\bdc fabric\b'

  - label: 园区网
    patterns:
      - '\bcampus\b|\benterprise\b'

  - label: 5GC
    patterns:
      - '\b5gc\b|\b5g core\b|\bamf\b|\bsmf\b'

  # ...
```

### relation_extraction.yaml

```yaml
# 关系抽取：正则捕获组 → (subject, predicate, object)
# group(1) = subject, group(2) = object
relations:
  - predicate: uses_protocol
    pattern: '(\b[\w\-]+)\s+uses?\s+(?:the\s+)?(\b[\w\-]+)\s+protocol'

  - predicate: is_a
    pattern: '(\b[\w\-]+)\s+is\s+(?:a\s+type\s+of|a\s+kind\s+of|an?\s+)(\b[\w\-]+)'

  - predicate: depends_on
    pattern: '(\b[\w\-]+)\s+depends?\s+on\s+(\b[\w\-]+)'

  # ...
```

### predicate_signals.yaml

```yaml
# 谓语信号：关键词 → predicate ID（用于共现策略）
signals:
  - predicate: uses_protocol
    patterns:
      - '\buses?\b|\busing\b|\butiliz'

  - predicate: depends_on
    patterns:
      - '\bdepends?\s+on\b|\brequir'

  - predicate: impacts
    patterns:
      - '\bimpact|affect'

  # ...
```

## 2.3 加载机制

在 `OntologyRegistry` 中新增 pattern 加载（和 nodes/aliases/relations 同层级）：

```python
class OntologyRegistry:
    def __init__(self):
        self.nodes = {}
        self.alias_map = {}
        self.relation_ids = set()
        # 新增
        self.semantic_role_patterns = []   # [(compiled_re, role_name)]
        self.context_signal_patterns = []  # [(compiled_re, label)]
        self.relation_extraction_patterns = []  # [(compiled_re, predicate)]
        self.predicate_signal_patterns = []    # [(compiled_re, predicate)]

    def _load_patterns(self):
        """Load and compile all pattern YAML files."""
        ...
```

各 Stage 从 `app.ontology` 获取已编译的正则列表，不自己定义。

## 2.4 代码变更

| 文件 | 改动 |
|------|------|
| `ontology/patterns/*.yaml` | 4 个新文件 |
| `src/ontology/registry.py` | 新增 `_load_patterns()` 方法，加载 + 编译正则 |
| `src/ontology/yaml_provider.py` | 暴露 pattern 列表给 Stage 使用 |
| `src/pipeline/stages/stage2_segment.py` | 删除 `_ROLE_PATTERNS` 硬编码，改为从 `ontology` 读取 |
| `src/pipeline/stages/stage3_align.py` | 删除 `_CONTEXT_PATTERNS` 硬编码，改为从 `ontology` 读取 |
| `src/pipeline/stages/stage4_extract.py` | 删除 `RELATION_PATTERNS` 和 `_PREDICATE_SIGNALS` 硬编码，改为从 `ontology` 读取 |

---

# 三、关键约束

1. **YAML 是唯一源头** — 和 nodes/aliases/relations 一样，patterns 也由 YAML 定义
2. **代码不包含领域知识** — Stage 代码只有"加载正则 → 匹配 → 处理结果"的通用逻辑
3. **本体变了不改代码** — 新增角色/场景/关系模式只需编辑 YAML
4. **正则在启动时编译一次** — `re.compile()` 在 OntologyRegistry 加载时执行，运行时无编译开销
5. **向后兼容** — 如果 patterns/ 目录不存在，回退到空列表（不会崩溃，但功能降级）