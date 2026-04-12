// ╔══════════════════════════════════════════════════════════════════════════╗
// ║         Telecom Semantic KB — Neo4j 常用查询手册                          ║
// ║  使用方法：在 Neo4j Browser (http://192.168.3.71:7474) 逐段粘贴执行        ║
// ╚══════════════════════════════════════════════════════════════════════════╝


// ════════════════════════════════════════════════════════════
// §1  全图快速概览
// ════════════════════════════════════════════════════════════

// 1-A  各类节点数量统计 —— 看整个知识库的规模
MATCH (n)
RETURN labels(n)[0] AS 节点类型, count(n) AS 数量
ORDER BY 数量 DESC;

// 1-B  各类关系数量统计
MATCH ()-[r]->()
RETURN type(r) AS 关系类型, count(r) AS 数量
ORDER BY 数量 DESC;

// 1-C  可视化全图（节点太多时建议限制数量）
// ⚠ 仅在本体已加载、知识较少时使用，数据量大时改用 §2-§6 的局部查询
MATCH (n)-[r]->(m)
RETURN n, r, m
LIMIT 200;


// ════════════════════════════════════════════════════════════
// §2  本体结构查询（五层本体，知识骨架）
// ════════════════════════════════════════════════════════════

// 2-A  查看五层本体各层节点数量 + 样例
// 说明：Concept = YANG可配置对象，Mechanism = 协议机制，Method = 操作方法，
//       Condition = 适用条件，Scenario = 业务场景
MATCH (n)
WHERE n:OntologyNode OR n:MechanismNode OR n:MethodNode
   OR n:ConditionRuleNode OR n:ScenarioPatternNode
RETURN
  CASE
    WHEN n:OntologyNode      THEN 'concept (可配置对象)'
    WHEN n:MechanismNode     THEN 'mechanism (协议机制)'
    WHEN n:MethodNode        THEN 'method (操作方法)'
    WHEN n:ConditionRuleNode THEN 'condition (适用条件)'
    WHEN n:ScenarioPatternNode THEN 'scenario (业务场景)'
  END AS 层次,
  count(n) AS 节点数,
  collect(n.canonical_name)[..5] AS 示例节点
ORDER BY 节点数 DESC;

// 2-B  某一概念的五层完整知识树 + 全部知识片段/事实/证据（以 BGP 为例）
// 说明：从概念层出发，沿五层本体链展开，同时展示链上每个节点关联的
//       Fact → Evidence → KnowledgeSegment → SourceDocument 完整证据链。
//       在 Neo4j Browser 中以图形模式查看，可看到完整的知识溯源网络。
// ⚠ 结果较大，如需缩小范围可把 CONTAINS 'BGP' 改为 = 'IP.BGP_INSTANCE'

// 从一个节点出发，沿五层本体链展开，展示链上每个节点及其
// Alias、Fact、Evidence、KnowledgeSegment、SourceDocument
// Neo4j Browser 开启 "Connect result nodes" 会自动绘制本体链边
// 可选起点（按链路大小排序）：
//   IP.VRRP_GROUP   — 五层齐全，最小链路 (1+1+1+1=4)
//   IP.LDP_INSTANCE — 四层 (1+1+1+0=3)
//   IP.BFD_SESSION  — 四层 (1+2+2+0=5)
//   IP.BGP_INSTANCE — 最大，展示完整但节点很多
MATCH (c:OntologyNode {node_id: 'IP.VRRP_GROUP'})
OPTIONAL MATCH p1 = (c)-[r1]-(mech:MechanismNode)
OPTIONAL MATCH p2 = (mech)-[r2]-(mt:MethodNode)
OPTIONAL MATCH p3 = (mt)-[r3]-(cond:ConditionRuleNode)
OPTIONAL MATCH p4 = (cond)-[r4]-(scene:ScenarioPatternNode)
// 收集链上所有节点（去null）
WITH c, mech, mt, cond, scene,
     p1, p2, p3, p4,
     [x IN [c, mech, mt, cond, scene] WHERE x IS NOT NULL] AS chain
UNWIND chain AS node
// 每个节点的别名
OPTIONAL MATCH pa = (alias:Alias)-[:ALIAS_OF]->(node)
// 只保留有完整证据链的 Fact（无证据的 Fact 没有图边，会孤立）
OPTIONAL MATCH pe = (ev:Evidence)-[:EXTRACTED_FROM]->(seg:KnowledgeSegment)
                      -[:BELONGS_TO]->(doc:SourceDocument)
WHERE EXISTS {
  MATCH (f2:Fact)-[:SUPPORTED_BY]->(ev)
  WHERE f2.subject = node.node_id OR f2.object = node.node_id
}
// Fact→Evidence 这条边也要返回才连通
OPTIONAL MATCH pf = (f:Fact)-[:SUPPORTED_BY]->(ev)
WHERE f.subject = node.node_id OR f.object = node.node_id
RETURN p1, p2, p3, p4, node, pa, pf, pe;

// 2-C  查询节点的所有别名（跨厂商、中英文）
// 说明：这是跨厂商术语归一化的核心 —— 同一概念在华为/Juniper/Arista中叫法不同
MATCH (a:Alias)-[:ALIAS_OF]->(n:OntologyNode)
WHERE n.canonical_name CONTAINS 'BGP'
RETURN
  n.canonical_name     AS 标准名称,
  n.display_name_zh    AS 中文名称,
  a.surface_form       AS 别名,
  a.alias_type         AS 别名类型,
  a.language           AS 语言,
  a.vendor             AS 厂商
ORDER BY a.vendor, a.language;

// 2-D  查询某对象的直接依赖（必须先配置什么才能用它）
// 说明：Concept层的 DEPENDS_ON 关系 = 工程师配置依赖顺序
MATCH (a:OntologyNode)-[:DEPENDS_ON]->(b:OntologyNode)
WHERE a.node_id CONTAINS 'BGP'
RETURN
  a.canonical_name AS 对象,
  a.display_name_zh AS 对象中文,
  b.canonical_name AS 依赖对象,
  b.display_name_zh AS 依赖对象中文,
  b.description    AS 说明;

// 2-E  五层跨层关系 —— 概念到机制到方法的完整知识链
// 说明：这是系统最核心的价值：把"什么"→"怎么工作"→"怎么配"串成一条线
MATCH path =
  (c:OntologyNode)-[:IMPLEMENTED_BY|EXPLAINS|APPLIES_TO*1..3]->(x)
WHERE c.node_id CONTAINS 'BGP'
RETURN path
LIMIT 80;


// ════════════════════════════════════════════════════════════
// §3  知识溯源 —— 从对象出发追踪证据链
// ════════════════════════════════════════════════════════════

// 3-A  查询某对象相关的所有已提取事实（三元组）
// 说明：这些三元组是从 RFC/厂商文档中自动抽取的，每条都有置信度和来源
MATCH (f:Fact)
WHERE f.subject CONTAINS 'BGP' OR f.object CONTAINS 'BGP'
RETURN
  f.subject    AS 主体,
  f.predicate  AS 关系,
  f.object     AS 客体,
  f.confidence AS 置信度,
  f.source     AS 来源
ORDER BY f.confidence DESC
LIMIT 30;

// 3-B  查询高置信度事实（置信度 ≥ 0.85）
// 说明：来自 IETF/IEEE 等 S 级权威来源的事实，置信度最高
MATCH (f:Fact)
WHERE f.confidence >= 0.85
RETURN
  f.subject    AS 主体,
  f.predicate  AS 关系,
  f.object     AS 客体,
  f.confidence AS 置信度,
  f.source     AS 来源
ORDER BY f.confidence DESC
LIMIT 20;

// 3-C  从知识片段追溯原始文档来源（完整溯源链）
// 说明：系统每条知识都能追溯到具体文档的具体段落——这是传统知识库做不到的
MATCH (seg:KnowledgeSegment)-[r]-(doc:SourceDocument)
RETURN
  seg.segment_id   AS 片段ID,
  left(seg.text, 100) AS 片段内容,
  doc.source_doc_id AS 文档ID,
  doc.title        AS 文档标题,
  doc.source_url   AS 来源URL
LIMIT 20;


// ════════════════════════════════════════════════════════════
// §4  故障影响链分析（系统独有能力）
// ════════════════════════════════════════════════════════════

// 4-A  查询某对象的上游依赖闭包（依赖它的所有对象）
// 说明：当 BGP Instance 故障时，哪些对象会受影响？
//       传统文档查询无法回答这个问题，图数据库 + 结构化依赖关系可以
MATCH path = (upstream)-[:DEPENDS_ON*1..5]->(target:OntologyNode)
WHERE target.node_id CONTAINS 'BGP_INSTANCE'
RETURN
  upstream.canonical_name AS 受影响对象,
  upstream.display_name_zh AS 中文名,
  length(path)            AS 依赖深度
ORDER BY 依赖深度;

// 4-B  查询故障传播路径（全路径可视化）
MATCH path = (a:OntologyNode)-[:DEPENDS_ON*1..4]->(b:OntologyNode)
WHERE b.node_id CONTAINS 'BGP_INSTANCE'
RETURN path
LIMIT 50;

// 4-C  查询配置对象的完整依赖树（双向：我依赖谁 + 谁依赖我）
MATCH (target:OntologyNode)
WHERE target.node_id = 'IP.BGP_INSTANCE'
OPTIONAL MATCH (target)-[:DEPENDS_ON]->(dep:OntologyNode)
OPTIONAL MATCH (affected:OntologyNode)-[:DEPENDS_ON]->(target)
RETURN
  target.canonical_name  AS 目标对象,
  collect(DISTINCT dep.canonical_name)      AS 我依赖,
  collect(DISTINCT affected.canonical_name) AS 依赖我;


// ════════════════════════════════════════════════════════════
// §5  知识治理与演化追踪（系统独有能力）
// ════════════════════════════════════════════════════════════

// 5-A  查看待审核的候选词（Pipeline 从文档中发现的新术语）
// 说明：系统自动从爬取的文档中发现人类未定义的新术语，并评分，等待人工审批
MATCH (c:CandidateConcept)
RETURN
  c.candidate_id    AS ID,
  c.surface_form    AS 候选术语,
  c.review_status   AS 审核状态,
  c.composite_score AS 综合评分,
  c.source_count    AS 出现文档数
ORDER BY c.composite_score DESC
LIMIT 20;

// 5-B  查看不同审核状态的候选词分布
MATCH (c:CandidateConcept)
RETURN
  c.review_status AS 状态,
  count(c)        AS 数量,
  round(avg(c.composite_score), 3) AS 平均评分
ORDER BY 数量 DESC;

// 5-C  查看高分候选词（评分 ≥ 0.65，有资格进入本体）
// 说明：这些术语已通过六道门控，理论上可以被人工确认后加入本体
MATCH (c:CandidateConcept)
WHERE c.composite_score >= 0.65
RETURN
  c.surface_form    AS 候选术语,
  c.composite_score AS 综合评分,
  c.review_status   AS 状态,
  c.source_count    AS 文档来源数
ORDER BY c.composite_score DESC;

// 5-D  查看本体版本历史
MATCH (v:OntologyVersion)
RETURN
  v.version_tag    AS 版本,
  v.loaded_at      AS 加载时间,
  v.node_count     AS 节点数,
  v.relation_count AS 关系数
ORDER BY v.loaded_at DESC;


// ════════════════════════════════════════════════════════════
// §6  跨层语义推理（系统独有能力）
// ════════════════════════════════════════════════════════════

// 6-A  从一个协议概念出发，找到完整的五层知识链
// 说明：普通知识库只有文档，这里是结构化的"概念→机制→方法→条件→场景"链路
// 用来给工程师解释：这个协议是什么、怎么工作、怎么配、什么时候用、在哪个场景用
MATCH (concept:OntologyNode)
WHERE concept.node_id CONTAINS 'BGP'
  AND concept.maturity_level = 'core'
OPTIONAL MATCH (concept)-[:IMPLEMENTED_BY]->(mech:MechanismNode)
OPTIONAL MATCH (concept)-[:EXPLAINS|APPLIES_TO*1..2]->(method:MethodNode)
OPTIONAL MATCH (concept)-[:APPLIES_TO]->(cond:ConditionRuleNode)
OPTIONAL MATCH (concept)-[:COMPOSED_OF|PART_OF]->(scene:ScenarioPatternNode)
RETURN
  concept.canonical_name  AS 配置对象,
  concept.description     AS 对象说明,
  collect(DISTINCT mech.canonical_name)  AS 底层机制,
  collect(DISTINCT method.canonical_name) AS 操作方法,
  collect(DISTINCT cond.canonical_name)  AS 适用条件,
  collect(DISTINCT scene.canonical_name) AS 应用场景
LIMIT 10;

// 6-B  查找所有具备完整五层知识的对象（最完整的知识节点）
// 说明：这些对象是知识库中"学得最全"的部分
MATCH (c:OntologyNode)
WHERE EXISTS { (c)-[:IMPLEMENTED_BY]->(:MechanismNode) }
  AND EXISTS { (c)-[:EXPLAINS|APPLIES_TO*1..2]->(:MethodNode) }
RETURN
  c.canonical_name    AS 对象名称,
  c.display_name_zh   AS 中文名,
  c.maturity_level    AS 成熟度
ORDER BY c.maturity_level;

// 6-C  查找知识孤岛（只有概念层、无任何跨层关系的节点）
// 说明：这些节点是知识库的薄弱点，需要补充文档来丰富
MATCH (c:OntologyNode)
WHERE NOT EXISTS { (c)-[:IMPLEMENTED_BY|EXPLAINS|APPLIES_TO|COMPOSED_OF]->() }
  AND NOT EXISTS { ()-[:IMPLEMENTED_BY|EXPLAINS|APPLIES_TO|COMPOSED_OF]->(c) }
  AND c.lifecycle_state = 'active'
RETURN
  c.canonical_name  AS 孤立节点,
  c.display_name_zh AS 中文名,
  c.description     AS 说明
ORDER BY c.maturity_level;


// ════════════════════════════════════════════════════════════
// §7  数据质量与系统健康度
// ════════════════════════════════════════════════════════════

// 7-A  知识入库进度（各状态文档数）
// 说明：整个 Pipeline 的漏斗：raw → processed → 最终入图
MATCH (doc:SourceDocument)
RETURN
  doc.status      AS 状态,
  count(doc)      AS 文档数,
  collect(left(doc.title, 30))[..3] AS 示例
ORDER BY 文档数 DESC;

// 7-B  每个来源的知识产出量（哪些文档贡献了多少三元组）
MATCH (seg:KnowledgeSegment)<-[:FROM_SEGMENT]-(fact:Fact)
MATCH (seg)-[:FROM_DOCUMENT]->(doc:SourceDocument)
RETURN
  doc.title        AS 文档,
  doc.source_rank  AS 权威等级,
  count(fact)      AS 产出事实数,
  round(avg(fact.confidence), 3) AS 平均置信度
ORDER BY 产出事实数 DESC
LIMIT 20;

// 7-C  置信度分布（知识质量快照）
MATCH (f:Fact)
RETURN
  CASE
    WHEN f.confidence >= 0.90 THEN '极高 (≥0.90)'
    WHEN f.confidence >= 0.75 THEN '高   (0.75-0.90)'
    WHEN f.confidence >= 0.60 THEN '中   (0.60-0.75)'
    ELSE                           '低   (<0.60)'
  END AS 置信度区间,
  count(f) AS 事实数
ORDER BY 事实数 DESC;

// 7-D  别名覆盖热力图（哪些本体节点有最多别名 = 跨厂商术语最混乱的概念）
// 说明：别名越多说明这个概念在行业里叫法越乱，归一化价值越高
MATCH (a:Alias)-[:ALIAS_OF]->(n:OntologyNode)
RETURN
  n.canonical_name    AS 标准名称,
  n.display_name_zh   AS 中文名,
  count(a)            AS 别名数量,
  collect(DISTINCT a.vendor)[..5] AS 涉及厂商
ORDER BY 别名数量 DESC
LIMIT 15;
