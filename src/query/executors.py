"""Five primitive executors for the query engine.

Each executor reads/writes WorkingMemory and operates on the HIN via
app.graph (Neo4j), app.store (PostgreSQL), app.ontology, and app.embedding.
"""

from __future__ import annotations

import logging
import time
from typing import Any, TYPE_CHECKING

from src.query.types import (
    MAX_CROSS_ENCODER,
    MAX_RESULT_SET,
    MAX_TRAVERSE_NODES,
    RESERVED_EDGES,
    NodeRef,
    ResultSet,
    StepTrace,
    WorkingMemory,
)

if TYPE_CHECKING:
    from semcore.app import SemanticApp

log = logging.getLogger(__name__)

SOURCE_RANK_SCORES = {"S": 1.0, "A": 0.85, "B": 0.65, "C": 0.40}


# ── helpers ──────────────────────────────────────────────────────────────────

def _node_ref_from_neo4j(row: dict, key: str = "m") -> NodeRef | None:
    """Extract a NodeRef from a Neo4j result row."""
    props = row.get(key)
    if props is not None and hasattr(props, "get"):
        p = dict(props)
        nid = p.get("node_id") or p.get("id", "")
        return NodeRef(node_id=nid, node_type="node", properties=p)
    nid = row.get("node_id") or row.get("id", "")
    if nid:
        return NodeRef(node_id=str(nid), node_type="node", properties=dict(row))
    return None


def _segment_ref(row: dict) -> NodeRef:
    sid = row.get("segment_id", "")
    return NodeRef(node_id=str(sid), node_type="segment", properties=dict(row))


def _fact_ref(row: dict) -> NodeRef:
    fid = row.get("fact_id", "")
    return NodeRef(node_id=str(fid), node_type="fact", properties=dict(row))


def _dedup_nodes(nodes: list[NodeRef]) -> list[NodeRef]:
    seen: set[tuple[str, str]] = set()
    result: list[NodeRef] = []
    for n in nodes:
        key = (n.node_id, n.node_type)
        if key not in seen:
            seen.add(key)
            result.append(n)
    return result


def _placeholders(n: int, cast: str = "") -> str:
    ph = f"%s{cast}" if cast else "%s"
    return ", ".join([ph] * n)


# ═════════════════════════════════════════════════════════════════════════════
# 1. SeedExecutor
# ═════════════════════════════════════════════════════════════════════════════

class SeedExecutor:
    def execute(self, step: dict, step_idx: int, wm: WorkingMemory, app: SemanticApp) -> None:
        by = step["by"]
        target = step["target"]
        value = step["value"]
        as_var = step["as"]

        handler = getattr(self, f"_seed_{by}", None)
        if handler is None:
            raise ValueError(f"Unknown seed mode: {by}")
        nodes = handler(value, target, step, app)
        rs = ResultSet(
            nodes=_dedup_nodes(nodes),
            provenance=[StepTrace(step_idx, "seed", as_var)],
        )
        rs.truncate(MAX_RESULT_SET)
        wm.put(as_var, rs)

    def _seed_id(self, value: Any, target: str, step: dict, app: SemanticApp) -> list[NodeRef]:
        ids = value if isinstance(value, list) else [value]
        if target == "node":
            return self._seed_nodes_by_id(ids, app)
        if target == "segment":
            return self._seed_segments_by_id(ids, app)
        if target == "fact":
            return self._seed_facts_by_id(ids, app)
        return []

    def _seed_nodes_by_id(self, ids: list[str], app: SemanticApp) -> list[NodeRef]:
        results: list[NodeRef] = []
        for nid in ids:
            cypher = "MATCH (n:OntologyNode {node_id: $id}) RETURN n LIMIT 1"
            rows = app.graph.read(cypher, id=nid)
            if rows:
                ref = _node_ref_from_neo4j(rows[0], "n")
                if ref:
                    results.append(ref)
        return results

    def _seed_segments_by_id(self, ids: list[str], app: SemanticApp) -> list[NodeRef]:
        if not ids:
            return []
        ph = ",".join("%s::uuid" for _ in ids)
        sql = f"SELECT * FROM segments WHERE segment_id IN ({ph}) AND lifecycle_state = 'active'"
        rows = app.store.fetchall(sql, tuple(ids))
        return [_segment_ref(r) for r in rows]

    def _seed_facts_by_id(self, ids: list[str], app: SemanticApp) -> list[NodeRef]:
        if not ids:
            return []
        ph = ",".join("%s::uuid" for _ in ids)
        sql = f"SELECT * FROM facts WHERE fact_id IN ({ph}) AND lifecycle_state = 'active'"
        rows = app.store.fetchall(sql, tuple(ids))
        return [_fact_ref(r) for r in rows]

    def _seed_alias(self, value: Any, target: str, step: dict, app: SemanticApp) -> list[NodeRef]:
        aliases = value if isinstance(value, list) else [value]
        results: list[NodeRef] = []
        for alias in aliases:
            node = app.ontology.resolve_alias(alias)
            if node:
                results.append(NodeRef(
                    node_id=node.node_id,
                    node_type="node",
                    properties={"canonical_name": node.label, "layer": node.layer.value, "domain": node.domain},
                ))
        return results

    def _seed_layer(self, value: Any, target: str, step: dict, app: SemanticApp) -> list[NodeRef]:
        from semcore.core.types import KnowledgeLayer
        try:
            layer = KnowledgeLayer(value)
        except ValueError:
            return []
        nodes = app.ontology.get_layer_nodes(layer)
        return [
            NodeRef(node_id=n.node_id, node_type="node",
                    properties={"canonical_name": n.label, "layer": n.layer.value, "domain": n.domain})
            for n in nodes
        ]

    def _seed_embedding(self, value: Any, target: str, step: dict, app: SemanticApp) -> list[NodeRef]:
        from src.config.settings import settings
        if not settings.EMBEDDING_ENABLED:
            return []
        from src.utils.embedding import embed_query, vector_to_pg_literal
        vec = embed_query(str(value))
        if vec is None:
            return []
        pg_vec = vector_to_pg_literal(vec)
        top_k = step.get("top_k", 100)
        threshold = step.get("threshold", 0.0)

        if target == "segment":
            sql = """
                SELECT s.segment_id, s.segment_type, s.section_title,
                       s.raw_text, s.normalized_text, s.token_count,
                       s.confidence, s.source_doc_id, s.lifecycle_state,
                       1 - (s.content_vec <=> %s::vector) AS similarity
                FROM segments s
                WHERE s.lifecycle_state = 'active' AND s.content_vec IS NOT NULL
                ORDER BY s.content_vec <=> %s::vector
                LIMIT %s
            """
            rows = app.store.fetchall(sql, (pg_vec, pg_vec, top_k))
            return [_segment_ref(r) for r in rows if float(r.get("similarity", 0)) >= threshold]

        if target == "node":
            sql = """
                SELECT s.segment_id, st.ontology_node_id,
                       1 - (s.content_vec <=> %s::vector) AS similarity
                FROM segments s
                JOIN segment_tags st ON s.segment_id = st.segment_id
                WHERE s.lifecycle_state = 'active' AND s.content_vec IS NOT NULL
                ORDER BY s.content_vec <=> %s::vector
                LIMIT %s
            """
            rows = app.store.fetchall(sql, (pg_vec, pg_vec, top_k))
            seen: set[str] = set()
            results: list[NodeRef] = []
            for r in rows:
                nid = r.get("ontology_node_id", "")
                if nid and nid not in seen and float(r.get("similarity", 0)) >= threshold:
                    seen.add(nid)
                    results.append(NodeRef(node_id=nid, node_type="node", properties={"similarity": r.get("similarity")}))
            return results

        return []

    def _seed_attribute(self, value: Any, target: str, step: dict, app: SemanticApp) -> list[NodeRef]:
        if not isinstance(value, dict) or not value:
            return []
        if target == "segment":
            conditions = []
            params: list[Any] = []
            for k, v in value.items():
                conditions.append(f"{k} = %s")
                params.append(v)
            where = " AND ".join(conditions)
            sql = f"SELECT * FROM segments WHERE lifecycle_state = 'active' AND {where}"
            rows = app.store.fetchall(sql, tuple(params))
            return [_segment_ref(r) for r in rows]
        if target == "fact":
            conditions = []
            params = []
            for k, v in value.items():
                conditions.append(f"{k} = %s")
                params.append(v)
            where = " AND ".join(conditions)
            sql = f"SELECT * FROM facts WHERE lifecycle_state = 'active' AND {where}"
            rows = app.store.fetchall(sql, tuple(params))
            return [_fact_ref(r) for r in rows]
        return []


# ═════════════════════════════════════════════════════════════════════════════
# 2. ExpandExecutor
# ═════════════════════════════════════════════════════════════════════════════

class ExpandExecutor:
    def execute(self, step: dict, step_idx: int, wm: WorkingMemory, app: SemanticApp) -> None:
        from_var = step["from"]
        as_var = step["as"]
        source_rs = wm.get(from_var)
        source_ids = list(source_rs.node_ids())

        if not source_ids:
            wm.put(as_var, ResultSet(provenance=[StepTrace(step_idx, "expand", as_var)]))
            return

        if "any_of" in step:
            nodes = self._expand_any_of(source_ids, source_rs, step, app)
        else:
            nodes = self._expand_sequence(source_ids, source_rs, step, app)

        rs = ResultSet(
            nodes=_dedup_nodes(nodes),
            provenance=[StepTrace(step_idx, "expand", as_var)],
        )
        rs.truncate(MAX_RESULT_SET)
        wm.put(as_var, rs)

    def _expand_any_of(
        self, source_ids: list[str], source_rs: ResultSet, step: dict, app: SemanticApp,
    ) -> list[NodeRef]:
        edge_types: list[str] = step["any_of"]
        direction = step.get("direction", "outbound")
        depth = step.get("depth", 1)
        decay = step.get("confidence_decay", 1.0)
        min_conf = step.get("min_confidence", 0.0)
        track_path = step.get("track_path", False)
        target = step.get("target")

        reserved = [e for e in edge_types if e in RESERVED_EDGES]
        ontology = [e for e in edge_types if e not in RESERVED_EDGES]

        results: list[NodeRef] = []

        if reserved:
            results.extend(self._expand_reserved(source_ids, source_rs, reserved, target, app))

        if ontology:
            results.extend(self._expand_graph_bfs(
                source_ids, ontology, direction, depth, decay, min_conf, track_path, app,
            ))

        return results

    def _expand_sequence(
        self, source_ids: list[str], source_rs: ResultSet, step: dict, app: SemanticApp,
    ) -> list[NodeRef]:
        seq: list[str] = step["sequence"]
        direction = step.get("direction", "outbound")
        current_ids = source_ids

        for edge_type in seq:
            if edge_type in RESERVED_EDGES:
                dummy_rs = ResultSet(nodes=[NodeRef(nid, "node") for nid in current_ids])
                refs = self._expand_reserved(current_ids, dummy_rs, [edge_type], None, app)
            else:
                refs = self._expand_graph_bfs(current_ids, [edge_type], direction, 1, 1.0, 0.0, False, app)
            current_ids = list({r.node_id for r in refs})
            if not current_ids:
                break

        if not current_ids:
            return []
        last_edge = seq[-1]
        if last_edge in RESERVED_EDGES:
            dummy_rs = ResultSet(nodes=[NodeRef(nid, "node") for nid in source_ids])
            return self._expand_reserved(current_ids, dummy_rs, [last_edge], None, app)
        cypher = "MATCH (n:OntologyNode) WHERE n.node_id IN $ids RETURN n"
        rows = app.graph.read(cypher, ids=current_ids)
        results = []
        for row in rows:
            ref = _node_ref_from_neo4j(row, "n")
            if ref:
                results.append(ref)
        return results

    def _expand_graph_bfs(
        self,
        start_ids: list[str],
        rel_types: list[str],
        direction: str,
        depth: int | str,
        decay: float,
        min_conf: float,
        track_path: bool,
        app: SemanticApp,
    ) -> list[NodeRef]:
        max_depth = MAX_TRAVERSE_NODES if depth == "unlimited" else depth
        rels = "|".join(r.upper() for r in rel_types)

        if direction == "outbound":
            pattern = f"(n)-[r:{rels}]->(m)"
        elif direction == "inbound":
            pattern = f"(n)<-[r:{rels}]-(m)"
        else:
            pattern = f"(n)-[r:{rels}]-(m)"

        visited: set[str] = set(start_ids)
        frontier = list(start_ids)
        all_found: list[NodeRef] = []
        current_conf = 1.0

        for hop in range(max_depth):
            if not frontier or len(visited) >= MAX_TRAVERSE_NODES:
                break
            cypher = f"""
                MATCH {pattern}
                WHERE n.node_id IN $ids
                RETURN m.node_id AS node_id, properties(m) AS props,
                       type(r) AS rel_type
            """
            rows = app.graph.read(cypher, ids=frontier)
            next_frontier: list[str] = []
            for row in rows:
                nid = row.get("node_id", "")
                if not nid or nid in visited:
                    continue
                current_conf *= decay
                if current_conf < min_conf:
                    continue
                visited.add(nid)
                props = row.get("props") or {}
                if isinstance(props, dict):
                    props["_hop"] = hop + 1
                    props["_confidence"] = current_conf
                    if track_path:
                        props["_rel_type"] = row.get("rel_type", "")
                all_found.append(NodeRef(node_id=nid, node_type="node", properties=props))
                next_frontier.append(nid)
            frontier = next_frontier

            if depth == "unlimited" and not next_frontier:
                break

        return all_found

    def _expand_reserved(
        self,
        source_ids: list[str],
        source_rs: ResultSet,
        edges: list[str],
        target: str | None,
        app: SemanticApp,
    ) -> list[NodeRef]:
        results: list[NodeRef] = []
        for edge in edges:
            if edge == "tagged_in":
                results.extend(self._expand_tagged_in(source_ids, app))
            elif edge == "rst_adjacent":
                results.extend(self._expand_rst_adjacent(source_ids, app))
            elif edge == "evidenced_by":
                results.extend(self._expand_evidenced_by(source_ids, app))
        return results

    def _expand_tagged_in(self, node_ids: list[str], app: SemanticApp) -> list[NodeRef]:
        if not node_ids:
            return []
        ph = _placeholders(len(node_ids))
        sql = f"""
            SELECT s.segment_id, s.raw_text, s.normalized_text, s.segment_type,
                   s.token_count, s.confidence, s.source_doc_id, s.section_title,
                   st.ontology_node_id AS matched_node
            FROM segments s
            JOIN segment_tags st ON s.segment_id = st.segment_id
            WHERE st.ontology_node_id IN ({ph})
              AND s.lifecycle_state = 'active'
        """
        rows = app.store.fetchall(sql, tuple(node_ids))
        return [_segment_ref(r) for r in rows]

    def _expand_rst_adjacent(self, segment_ids: list[str], app: SemanticApp) -> list[NodeRef]:
        if not segment_ids:
            return []
        ph = _placeholders(len(segment_ids), "::uuid")
        sql = f"""
            SELECT DISTINCT
                CASE WHEN r.src_edu_id IN ({ph}) THEN r.dst_edu_id ELSE r.src_edu_id END AS segment_id,
                r.relation_type
            FROM t_rst_relation r
            WHERE r.src_edu_id IN ({ph}) OR r.dst_edu_id IN ({ph})
        """
        params = tuple(segment_ids) * 3
        rows = app.store.fetchall(sql, params)
        id_set = set(segment_ids)
        results: list[NodeRef] = []
        for r in rows:
            sid = r.get("segment_id", "")
            if sid and sid not in id_set:
                results.append(NodeRef(
                    node_id=str(sid), node_type="segment",
                    properties={"rst_relation": r.get("relation_type", "")},
                ))
        return results

    def _expand_evidenced_by(self, fact_ids: list[str], app: SemanticApp) -> list[NodeRef]:
        if not fact_ids:
            return []
        ph = _placeholders(len(fact_ids), "::uuid")
        # DISTINCT ON (s.segment_id): one segment may support multiple facts,
        # so without dedup the same segment would appear once per fact_id.
        # Keep the evidence row with the highest score for each segment.
        sql = f"""
            SELECT DISTINCT ON (s.segment_id)
                   s.segment_id, s.raw_text, s.normalized_text, s.segment_type,
                   s.token_count, s.confidence, s.source_doc_id,
                   e.fact_id, e.evidence_score, e.extraction_method
            FROM evidence e
            JOIN segments s ON e.segment_id = s.segment_id
            WHERE e.fact_id IN ({ph})
              AND s.lifecycle_state = 'active'
            ORDER BY s.segment_id, e.evidence_score DESC
        """
        rows = app.store.fetchall(sql, tuple(fact_ids))
        return [_segment_ref(r) for r in rows]


# ═════════════════════════════════════════════════════════════════════════════
# 3. CombineExecutor
# ═════════════════════════════════════════════════════════════════════════════

class CombineExecutor:
    def execute(self, step: dict, step_idx: int, wm: WorkingMemory, app: SemanticApp) -> None:
        method = step["method"]
        set_vars: list[str] = step["sets"]
        as_var = step["as"]

        sets = [wm.get(v) for v in set_vars]

        if method == "union":
            nodes = self._union(sets)
        elif method == "intersect":
            nodes = self._intersect(sets)
        elif method == "subtract":
            nodes = self._subtract(sets[0], sets[1])
        else:
            nodes = []

        rs = ResultSet(
            nodes=nodes,
            provenance=[StepTrace(step_idx, "combine", as_var)],
        )
        rs.truncate(MAX_RESULT_SET)
        wm.put(as_var, rs)

    def _union(self, sets: list[ResultSet]) -> list[NodeRef]:
        all_nodes: list[NodeRef] = []
        for s in sets:
            all_nodes.extend(s.nodes)
        return _dedup_nodes(all_nodes)

    def _intersect(self, sets: list[ResultSet]) -> list[NodeRef]:
        if not sets:
            return []
        id_sets = [s.node_ids() for s in sets]
        common = id_sets[0]
        for ids in id_sets[1:]:
            common = common & ids
        return [n for n in sets[0].nodes if n.node_id in common]

    def _subtract(self, a: ResultSet, b: ResultSet) -> list[NodeRef]:
        b_ids = b.node_ids()
        return [n for n in a.nodes if n.node_id not in b_ids]


# ═════════════════════════════════════════════════════════════════════════════
# 4. AggregateExecutor
# ═════════════════════════════════════════════════════════════════════════════

class AggregateExecutor:
    def execute(self, step: dict, step_idx: int, wm: WorkingMemory, app: SemanticApp, plan: dict | None = None) -> None:
        func = step["function"]
        from_var = step["from"]
        as_var = step["as"]
        source = wm.get(from_var)

        handler = getattr(self, f"_agg_{func}", None)
        if handler is None:
            raise ValueError(f"Unknown aggregate function: {func}")
        result = handler(source, step, app, plan, wm)

        if isinstance(result, ResultSet):
            result.provenance.append(StepTrace(step_idx, "aggregate", as_var))
            result.truncate(MAX_RESULT_SET)
            wm.put(as_var, result)
        else:
            wm.put(as_var, ResultSet(
                metadata={"value": result},
                provenance=[StepTrace(step_idx, "aggregate", as_var)],
            ))

    def _agg_count(self, source: ResultSet, step: dict, app: SemanticApp, plan: dict | None, wm: WorkingMemory) -> int:
        return len(source.nodes)

    def _agg_rank(self, source: ResultSet, step: dict, app: SemanticApp, plan: dict | None, wm: WorkingMemory) -> ResultSet:
        by_fields: list[str] = step.get("by", ["confidence"])
        order = step.get("order", "desc")
        limit = step.get("limit")
        reverse = order == "desc"

        def _coerce(val: Any) -> Any:
            if val is None:
                return ""
            try:
                return float(val)
            except (ValueError, TypeError):
                return str(val)

        def sort_key(n: NodeRef) -> tuple:
            return tuple(_coerce(n.properties.get(f)) for f in by_fields)

        sorted_nodes = sorted(source.nodes, key=sort_key, reverse=reverse)
        if limit:
            sorted_nodes = sorted_nodes[:limit]
        return ResultSet(nodes=sorted_nodes)

    def _agg_group(self, source: ResultSet, step: dict, app: SemanticApp, plan: dict | None, wm: WorkingMemory) -> ResultSet:
        by_fields: list[str] = step.get("by", [])
        groups: dict[tuple, list[NodeRef]] = {}
        for n in source.nodes:
            key = tuple(n.properties.get(f, "") for f in by_fields)
            groups.setdefault(key, []).append(n)
        return ResultSet(
            nodes=source.nodes,
            metadata={"groups": {str(k): len(v) for k, v in groups.items()}},
        )

    def _agg_score(self, source: ResultSet, step: dict, app: SemanticApp, plan: dict | None, wm: WorkingMemory) -> ResultSet:
        signals: list[str] = step.get("signals", [])
        for n in source.nodes:
            total = sum(float(n.properties.get(s, 0) or 0) for s in signals)
            n.properties["_score"] = total / max(len(signals), 1)
        order = step.get("order", "desc")
        reverse = order == "desc"
        sorted_nodes = sorted(source.nodes, key=lambda n: n.properties.get("_score", 0), reverse=reverse)
        limit = step.get("limit")
        if limit:
            sorted_nodes = sorted_nodes[:limit]
        return ResultSet(nodes=sorted_nodes)

    def _agg_rerank(self, source: ResultSet, step: dict, app: SemanticApp, plan: dict | None, wm: WorkingMemory) -> ResultSet:
        signals: list[str] = step.get("signals", ["source_rank", "confidence", "anchor_coverage", "rst_coherence", "freshness"])
        use_ce = step.get("cross_encoder", False)
        query_text = step.get("query") or (plan or {}).get("intent") or self._auto_query(plan, wm)
        budget = step.get("budget")
        limit = step.get("limit", 30)

        # Phase 4a: feature scoring + keyword relevance
        self._phase_4a_score(source.nodes, signals, wm, query_text)
        scored = sorted(source.nodes, key=lambda n: n.properties.get("_feature_score", 0), reverse=True)

        # Phase 4b: cross-encoder
        if use_ce and query_text:
            ce_count = min(MAX_CROSS_ENCODER, len(scored))
            top_for_ce = scored[:ce_count]
            self._phase_4b_cross_encoder(top_for_ce, query_text)
            scored = sorted(scored, key=lambda n: n.properties.get("_final_score", n.properties.get("_feature_score", 0)), reverse=True)

        # Phase 4c: budget-aware selection
        if budget:
            scored = self._phase_4c_budget_select(scored, budget, limit)
        elif limit:
            scored = scored[:limit]

        return ResultSet(nodes=scored)

    def _phase_4a_score(self, nodes: list[NodeRef], signals: list[str], wm: WorkingMemory, query_text: str = "") -> None:
        weights = {
            "source_rank": 0.15,
            "confidence": 0.15,
            "anchor_coverage": 0.15,
            "rst_coherence": 0.10,
            "freshness": 0.10,
        }
        query_terms = [t.lower() for t in query_text.split() if len(t) >= 2] if query_text else []
        for n in nodes:
            score = 0.0
            for sig in signals:
                w = weights.get(sig, 1.0 / max(len(signals), 1))
                if sig == "source_rank":
                    rank_val = str(n.properties.get("source_rank", "C"))
                    val = SOURCE_RANK_SCORES.get(rank_val, 0.40)
                else:
                    val = float(n.properties.get(sig, 0) or 0)
                score += w * val
            if query_terms:
                text = (n.properties.get("raw_text") or n.properties.get("normalized_text") or "").lower()
                hits = sum(1 for t in query_terms if t in text)
                keyword_score = min(hits / max(len(query_terms), 1), 1.0)
                score += 0.35 * keyword_score
            n.properties["_feature_score"] = round(score, 4)

    def _phase_4b_cross_encoder(self, nodes: list[NodeRef], query: str) -> None:
        from src.query.reranker import rerank_pairs
        passages = [n.properties.get("raw_text") or n.properties.get("normalized_text") or "" for n in nodes]
        scores = rerank_pairs(query, passages)
        if scores:
            for n, ce_score in zip(nodes, scores):
                feat = n.properties.get("_feature_score", 0.0)
                n.properties["_ce_score"] = ce_score
                n.properties["_final_score"] = 0.4 * feat + 0.6 * (ce_score / 10.0)
        else:
            for n in nodes:
                n.properties["_final_score"] = n.properties.get("_feature_score", 0.0)

    def _phase_4c_budget_select(self, nodes: list[NodeRef], budget: int, limit: int) -> list[NodeRef]:
        selected: list[NodeRef] = []
        token_count = 0
        for n in nodes:
            if len(selected) >= limit:
                break
            tokens = int(n.properties.get("token_count", 0) or 0)
            if tokens == 0:
                text = n.properties.get("raw_text") or n.properties.get("normalized_text") or ""
                tokens = max(1, len(text) // 4)
            if token_count + tokens > budget:
                continue
            selected.append(n)
            token_count += tokens
        return selected

    def _auto_query(self, plan: dict | None, wm: WorkingMemory) -> str:
        if not plan:
            return ""
        parts: list[str] = []
        for step in plan.get("steps", []):
            if step.get("op") == "seed" and "value" in step:
                v = step["value"]
                if isinstance(v, list):
                    parts.extend(str(x) for x in v)
                else:
                    parts.append(str(v))
        return " ".join(parts)


# ═════════════════════════════════════════════════════════════════════════════
# 5. ProjectExecutor
# ═════════════════════════════════════════════════════════════════════════════

class ProjectExecutor:
    def execute(self, step: dict, step_idx: int, wm: WorkingMemory, app: SemanticApp) -> None:
        from_var = step["from"]
        fields: list[str] = step["fields"]
        as_var = step["as"]
        source = wm.get(from_var)

        projected: list[NodeRef] = []
        for n in source.nodes:
            props = {}
            for f in fields:
                if f == "node_id" or f == "segment_id" or f == "fact_id":
                    props[f] = n.node_id
                elif f in n.properties:
                    props[f] = n.properties[f]
            projected.append(NodeRef(node_id=n.node_id, node_type=n.node_type, properties=props))

        rs = ResultSet(
            nodes=projected,
            provenance=[StepTrace(step_idx, "project", as_var)],
        )
        wm.put(as_var, rs)