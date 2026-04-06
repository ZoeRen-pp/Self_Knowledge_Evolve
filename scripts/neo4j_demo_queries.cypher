// ══════════════════════════════════════════════════════════════════════
// 电信语义知识操作系统 — Neo4j 展示查询集
// 在 Neo4j Browser (http://localhost:7474) 中逐条执行
// ══════════════════════════════════════════════════════════════════════


// ── 1. 全局概览：图数据库中有什么 ──────────────────────────────────

// 1a. 节点类型和数量
MATCH (n)
RETURN labels(n) AS 节点类型, count(n) AS 数量
ORDER BY 数量 DESC;

// 1b. 关系类型和数量（Top 20）
MATCH ()-[r]->()
RETURN type(r) AS 关系类型, count(r) AS 数量
ORDER BY 数量 DESC LIMIT 20;

// 1c. 全图规模
MATCH (n) WITH count(n) AS nodes
MATCH ()-[r]->() WITH nodes, count(r) AS rels
RETURN nodes AS 总节点数, rels AS 总关系数;


// ── 2. 五层本体模型 ────────────────────────────────────────────────

// 2a. 五层节点分布（核心！展示五层架构）
MATCH (n) WHERE n.lifecycle_state = 'active'
  AND (n:OntologyNode OR n:MechanismNode OR n:MethodNode
       OR n:ConditionRuleNode OR n:ScenarioPatternNode)
WITH labels(n)[0] AS 层,
     CASE labels(n)[0]
       WHEN 'OntologyNode' THEN '1-概念层'
       WHEN 'MechanismNode' THEN '2-机制层'
       WHEN 'MethodNode' THEN '3-方法层'
       WHEN 'ConditionRuleNode' THEN '4-条件层'
       WHEN 'ScenarioPatternNode' THEN '5-场景层'
     END AS 层名称,
     n
RETURN 层名称, count(n) AS 节点数
ORDER BY 层名称;

// 2b. 五层本体全景图（图可视化，限制 200 节点）
MATCH (n)-[r]-(m)
WHERE (n:OntologyNode OR n:MechanismNode OR n:MethodNode
       OR n:ConditionRuleNode OR n:ScenarioPatternNode)
  AND (m:OntologyNode OR m:MechanismNode OR m:MethodNode
       OR m:ConditionRuleNode OR m:ScenarioPatternNode)
  AND n.lifecycle_state = 'active'
RETURN n, r, m LIMIT 200;

// 2c. 概念层层级树（SUBCLASS_OF 关系）
MATCH (child:OntologyNode)-[:SUBCLASS_OF]->(parent:OntologyNode)
RETURN parent.canonical_name AS 父概念,
       collect(child.canonical_name) AS 子概念,
       count(child) AS 子节点数
ORDER BY 子节点数 DESC;


// ── 3. 单概念深度钻取（以 OSPF 为例） ─────────────────────────────

// 3a. OSPF 节点 + 所有直接关系（图可视化）
MATCH (n:OntologyNode {node_id: 'IP.OSPF'})-[r]-(m)
RETURN n, r, m;

// 3b. OSPF 的五层推理链（概念→机制→方法→条件→场景）
MATCH (c:OntologyNode {node_id: 'IP.OSPF'})-[r1]-(mech:MechanismNode)
OPTIONAL MATCH (mech)-[r2]-(meth:MethodNode)
OPTIONAL MATCH (meth)-[r3]-(cond:ConditionRuleNode)
OPTIONAL MATCH (meth)-[r4]-(scene:ScenarioPatternNode)
RETURN c, r1, mech, r2, meth, r3, cond, r4, scene;

// 3c. OSPF 的所有别名
MATCH (a:Alias)-[:ALIAS_OF]->(n:OntologyNode {node_id: 'IP.OSPF'})
RETURN a.surface_form AS 别名;

// 3d. OSPF 关联的知识条目（Facts）
MATCH (n:OntologyNode {node_id: 'IP.OSPF'})<-[:TAGGED_WITH]-(seg:KnowledgeSegment)
      -[:EXTRACTED_FROM]->(f:Fact)
RETURN f.subject AS 主语, f.predicate AS 谓语, f.object AS 宾语,
       f.confidence AS 置信度
ORDER BY f.confidence DESC LIMIT 20;


// ── 4. 以 BGP 为例展示跨概念关系网 ────────────────────────────────

// 4a. BGP 的二跳邻居网络
MATCH path = (n:OntologyNode {node_id: 'IP.BGP'})-[*1..2]-(m)
WHERE m:OntologyNode OR m:MechanismNode OR m:MethodNode
RETURN path LIMIT 100;

// 4b. BGP 到 MPLS 的最短路径
MATCH p = shortestPath(
    (a:OntologyNode {node_id: 'IP.BGP'})-[*..6]-
    (b:OntologyNode {node_id: 'IP.MPLS'})
)
RETURN p;

// 4c. BGP 依赖链（所有 DEPENDS_ON / USES_PROTOCOL 关系）
MATCH path = (n:OntologyNode {node_id: 'IP.BGP'})-[:DEPENDS_ON|USES_PROTOCOL*1..3]->(m)
RETURN path;


// ── 5. 知识条目和证据链溯源 ───────────────────────────────────────

// 5a. 高置信度知识条目 Top 20
MATCH (f:Fact)
WHERE f.lifecycle_state = 'active' AND f.confidence >= 0.7
RETURN f.subject AS 主语, f.predicate AS 谓语, f.object AS 宾语,
       f.confidence AS 置信度
ORDER BY f.confidence DESC LIMIT 20;

// 5b. 完整证据链溯源（Fact → Evidence → Segment → Document）
MATCH (f:Fact {lifecycle_state: 'active'})-[:SUPPORTED_BY]->(e:Evidence)
      -[:EXTRACTED_FROM]->(seg:KnowledgeSegment)-[:BELONGS_TO]->(doc:SourceDocument)
RETURN f.subject AS 主语, f.predicate AS 谓语, f.object AS 宾语,
       f.confidence AS 置信度, doc.title AS 来源文档
ORDER BY f.confidence DESC LIMIT 15;

// 5c. 特定谓语的知识条目（如 USES_PROTOCOL）
MATCH (a)-[r:USES_PROTOCOL]->(b)
RETURN a.canonical_name AS 使用者, r.predicate AS 关系, b.canonical_name AS 协议,
       r.confidence AS 置信度, r.fact_count AS 支撑条数
ORDER BY r.fact_count DESC LIMIT 20;


// ── 6. 跨层分析 ───────────────────────────────────────────────────

// 6a. 机制层 → 方法层连接（哪些机制有实施方法）
MATCH (mech:MechanismNode)-[r]-(meth:MethodNode)
RETURN mech.canonical_name AS 机制, type(r) AS 关系, meth.canonical_name AS 方法;

// 6b. 方法层 → 条件层连接（方法有什么适用条件）
MATCH (meth:MethodNode)-[r]-(cond:ConditionRuleNode)
RETURN meth.canonical_name AS 方法, type(r) AS 关系, cond.canonical_name AS 条件;

// 6c. 完整五层路径（概念→机制→方法→条件→场景）
MATCH (c:OntologyNode)-[r1]-(m:MechanismNode)
      -[r2]-(mt:MethodNode)-[r3]-(cn:ConditionRuleNode)
      -[r4]-(s:ScenarioPatternNode)
WHERE c.lifecycle_state = 'active'
RETURN c.canonical_name AS 概念,
       m.canonical_name AS 机制,
       mt.canonical_name AS 方法,
       cn.canonical_name AS 条件,
       s.canonical_name AS 场景
LIMIT 20;

// 6d. 场景层全景（所有场景及关联的方法）
MATCH (s:ScenarioPatternNode)-[r]-(m)
WHERE m:MethodNode OR m:OntologyNode
RETURN s, r, m;


// ── 7. 演化节点（审批通过的新概念） ──────────────────────────────

// 7a. 所有演化节点
MATCH (n:OntologyNode)
WHERE n.maturity_level = 'evolved'
RETURN n.node_id AS 节点ID, n.canonical_name AS 名称,
       n.approved AS 已审批, n.source_count AS 来源数
ORDER BY n.source_count DESC;

// 7b. 演化节点与父节点的关系
MATCH (n:OntologyNode {maturity_level: 'evolved'})-[:SUBCLASS_OF]->(p:OntologyNode)
RETURN n.canonical_name AS 新概念, p.canonical_name AS 父概念;

// 7c. 演化节点的图可视化
MATCH (n:OntologyNode {maturity_level: 'evolved'})-[r]-(m)
RETURN n, r, m;


// ── 8. 图结构分析 ─────────────────────────────────────────────────

// 8a. 度数最高的节点（Hub 节点）
MATCH (n)-[r]-()
WHERE n:OntologyNode OR n:MechanismNode
WITH n, count(r) AS degree
RETURN n.canonical_name AS 节点, labels(n)[0] AS 类型, degree AS 度数
ORDER BY degree DESC LIMIT 15;

// 8b. 孤立节点（没有本体关系的概念）
MATCH (n:OntologyNode)
WHERE n.lifecycle_state = 'active'
  AND NOT (n)-[:DEPENDS_ON|USES_PROTOCOL|SUBCLASS_OF|PART_OF]-()
RETURN n.node_id AS 节点ID, n.canonical_name AS 名称;

// 8c. 文档-段落-知识条目 数据流可视化（取一篇文档）
MATCH (doc:SourceDocument)<-[:BELONGS_TO]-(seg:KnowledgeSegment)
      -[:EXTRACTED_FROM]->(f:Fact)
WITH doc, count(DISTINCT seg) AS 段落数, count(DISTINCT f) AS 知识条目数
RETURN doc.title AS 文档, 段落数, 知识条目数
ORDER BY 知识条目数 DESC LIMIT 10;


// ── 9. 组合展示查询（适合截图/录屏） ──────────────────────────────

// 9a. 路由协议家族全景（BGP + OSPF + IS-IS 及关联）
MATCH (n:OntologyNode)-[r]-(m)
WHERE n.node_id IN ['IP.BGP', 'IP.OSPF', 'IP.ISIS', 'IP.ROUTING_PROTOCOL',
                     'IP.MPLS', 'IP.L3_ROUTING']
  AND (m:OntologyNode OR m:MechanismNode OR m:MethodNode)
RETURN n, r, m;

// 9b. EVPN-VXLAN 技术栈（Overlay 全景）
MATCH (n:OntologyNode)-[r]-(m)
WHERE n.node_id IN ['IP.EVPN', 'IP.VXLAN', 'IP.VPN_OVERLAY', 'IP.VTEP']
  AND (m:OntologyNode OR m:MechanismNode OR m:ScenarioPatternNode)
RETURN n, r, m;

// 9c. 别名系统可视化（展示跨厂商术语归一化）
MATCH (a:Alias)-[:ALIAS_OF]->(n:OntologyNode)
WHERE n.node_id IN ['IP.BGP', 'IP.OSPF', 'IP.MPLS', 'IP.VXLAN', 'IP.BFD']
RETURN n, a;
