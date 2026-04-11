"""Ontology quality calculator — 5 dimensions, 20 indicators.

Computes structural health metrics from knowledge data to audit ontology design.
Does not modify any data — read-only analysis.
"""

from __future__ import annotations

import logging
import math
from collections import Counter

log = logging.getLogger(__name__)


class OntologyQualityCalculator:
    """Compute all quality indicators from PG + Neo4j."""

    def __init__(self, store, graph):
        self._store = store
        self._graph = graph

    def compute_all(self) -> dict:
        """Return full quality report: 5 dimensions + overall score + diagnostics."""
        log.info("Computing ontology quality indicators...")

        granularity = self._granularity()
        orthogonality = self._orthogonality()
        cross_layer = self._cross_layer()
        discoverability = self._discoverability()
        structural = self._structural()

        # Dimension scores (0-1, higher is healthier)
        scores = {
            "granularity": granularity.get("score", 0),
            "orthogonality": orthogonality.get("score", 0),
            "cross_layer": cross_layer.get("score", 0),
            "discoverability": discoverability.get("score", 0),
            "structural": structural.get("score", 0),
        }
        overall = sum(scores.values()) / max(len(scores), 1)

        # Collect all issues
        issues = []
        for dim in [granularity, orthogonality, cross_layer, discoverability, structural]:
            issues.extend(dim.get("issues", []))

        result = {
            "granularity": granularity,
            "orthogonality": orthogonality,
            "cross_layer": cross_layer,
            "discoverability": discoverability,
            "structural": structural,
            "scores": scores,
            "overall_score": round(overall, 4),
            "issues": issues,
            "issue_count": len(issues),
        }
        log.info("Ontology quality: overall=%.2f issues=%d", overall, len(issues))
        return result

    # ── G: Granularity ────────────────────────────────────────────

    def _granularity(self) -> dict:
        g = self._graph
        s = self._store
        issues = []

        # G1: Degree Gini coefficient
        deg_rows = g.read(
            """MATCH (n:OntologyNode) WHERE n.lifecycle_state='active'
               OPTIONAL MATCH (n)-[r WHERE r.predicate IS NOT NULL]-()
               WITH n, count(r) AS degree
               RETURN degree ORDER BY degree"""
        )
        degrees = [r["degree"] for r in deg_rows]
        gini = self._gini(degrees) if degrees else 0
        if gini > 0.8:
            issues.append({"type": "high_gini", "value": gini,
                           "suggestion": "Few super-nodes dominate; consider splitting"})

        # G2: Super node ratio
        if degrees:
            mean_d = sum(degrees) / len(degrees)
            std_d = math.sqrt(sum((d - mean_d) ** 2 for d in degrees) / max(len(degrees), 1))
            threshold = mean_d + 3 * std_d
            super_nodes = [d for d in degrees if d > threshold]
            super_ratio = len(super_nodes) / max(len(degrees), 1)
        else:
            super_ratio = 0
            threshold = 0
        if super_ratio > 0.1:
            issues.append({"type": "super_node_ratio", "value": round(super_ratio, 4),
                           "suggestion": "Over 10% nodes are super-nodes"})

        # G3: Isolated node ratio
        isolated = sum(1 for d in degrees if d == 0)
        iso_ratio = isolated / max(len(degrees), 1)
        if iso_ratio > 0.4:
            issues.append({"type": "high_isolation", "value": round(iso_ratio, 4),
                           "suggestion": "Over 40% nodes have no knowledge edges"})

        # G4: Average tags per segment
        tag_row = s.fetchone(
            """SELECT AVG(tag_count) AS avg_tags FROM (
                SELECT segment_id, count(*) AS tag_count
                FROM segment_tags WHERE tag_type='canonical'
                GROUP BY segment_id) sub"""
        )
        avg_tags = float(tag_row["avg_tags"] or 0) if tag_row else 0
        if avg_tags > 8:
            issues.append({"type": "tag_overload", "value": round(avg_tags, 2),
                           "suggestion": "Segments avg >8 tags; node boundaries may be unclear"})

        # G5: Ubiquitous nodes
        total_segs_row = s.fetchone("SELECT count(DISTINCT segment_id) AS cnt FROM segment_tags")
        total_segs = total_segs_row["cnt"] if total_segs_row else 1
        ubiq_rows = s.fetchall(
            """SELECT ontology_node_id, count(DISTINCT segment_id) AS seg_count
               FROM segment_tags WHERE tag_type='canonical' AND ontology_node_id IS NOT NULL
               GROUP BY ontology_node_id
               HAVING count(DISTINCT segment_id) > %s * 0.8""",
            (total_segs,),
        )
        ubiq_nodes = [r["ontology_node_id"] for r in ubiq_rows]
        if len(ubiq_nodes) > 5:
            issues.append({"type": "ubiquitous_nodes", "nodes": ubiq_nodes,
                           "suggestion": "These nodes appear in >80% of segments; too broad"})

        # Score: fewer issues = healthier
        penalty = min(len(issues) * 0.15, 0.6)
        score = max(0, 1.0 - penalty - (0.1 if gini > 0.7 else 0) - (0.1 if iso_ratio > 0.3 else 0))

        return {
            "G1_gini": round(gini, 4),
            "G2_super_ratio": round(super_ratio, 4),
            "G3_isolation_ratio": round(iso_ratio, 4),
            "G4_avg_tags_per_segment": round(avg_tags, 2),
            "G5_ubiquitous_nodes": ubiq_nodes,
            "score": round(score, 4),
            "issues": issues,
        }

    # ── O: Orthogonality ──────────────────────────────────────────

    def _orthogonality(self) -> dict:
        s = self._store
        issues = []

        # O1: Predicate co-occurrence Jaccard
        facts = s.fetchall(
            "SELECT subject, predicate, object FROM facts WHERE lifecycle_state='active'"
        )
        pair_predicates: dict[tuple, set] = {}
        for f in facts:
            key = (f["subject"], f["object"])
            pair_predicates.setdefault(key, set()).add(f["predicate"])

        pred_pairs_jaccard: dict[tuple, float] = {}
        all_preds = set()
        for preds in pair_predicates.values():
            all_preds.update(preds)

        pred_list = sorted(all_preds)
        for i, p1 in enumerate(pred_list):
            for p2 in pred_list[i + 1:]:
                pairs_with_p1 = {k for k, ps in pair_predicates.items() if p1 in ps}
                pairs_with_p2 = {k for k, ps in pair_predicates.items() if p2 in ps}
                intersection = len(pairs_with_p1 & pairs_with_p2)
                union = len(pairs_with_p1 | pairs_with_p2)
                if union > 0:
                    jaccard = intersection / union
                    if jaccard > 0.3:
                        pred_pairs_jaccard[(p1, p2)] = round(jaccard, 4)

        overlapping = {f"{k[0]} ↔ {k[1]}": v for k, v in pred_pairs_jaccard.items() if v > 0.5}
        for pair_key, jac in sorted(overlapping.items(), key=lambda x: -x[1])[:5]:
            issues.append({"type": "predicate_overlap", "pair": pair_key, "jaccard": jac,
                           "suggestion": f"Semantic overlap: {pair_key}"})

        # O2: Predicate distribution skew
        pred_counts = Counter(f["predicate"] for f in facts)
        total_facts = sum(pred_counts.values()) or 1
        top3 = sum(c for _, c in pred_counts.most_common(3))
        top3_ratio = top3 / total_facts
        if top3_ratio > 0.8:
            issues.append({"type": "predicate_skew", "top3_ratio": round(top3_ratio, 4),
                           "suggestion": "Top 3 predicates dominate; relation types too broad"})

        # O3: Per-node predicate concentration
        node_pred_counts: dict[str, Counter] = {}
        for f in facts:
            node_pred_counts.setdefault(f["subject"], Counter())[f["predicate"]] += 1
        concentrated = []
        for node, preds in node_pred_counts.items():
            total = sum(preds.values())
            if total >= 5:
                top_pred, top_count = preds.most_common(1)[0]
                if top_count / total > 0.9:
                    concentrated.append({"node": node, "predicate": top_pred,
                                         "concentration": round(top_count / total, 2)})
        if len(concentrated) > 5:
            issues.append({"type": "predicate_concentration", "count": len(concentrated),
                           "suggestion": "Many nodes have >90% single-predicate edges"})

        # O4: Effective predicate count
        from src.ontology.registry import OntologyRegistry
        reg = OntologyRegistry.from_default()
        defined = len(reg.relation_ids)
        used = len(pred_counts)
        utilization = used / max(defined, 1)
        if utilization < 0.3:
            issues.append({"type": "low_utilization", "used": used, "defined": defined,
                           "suggestion": f"Only {used}/{defined} relation types used"})

        # O5: Node semantic similarity (neighborhood overlap + tag co-occurrence)
        similar_pairs = self._detect_similar_nodes()
        if len(similar_pairs) > 3:
            issues.append({
                "type": "similar_nodes", "count": len(similar_pairs),
                "suggestion": f"{len(similar_pairs)} node pairs with high semantic similarity — consider merging",
            })

        penalty = min(len(issues) * 0.15, 0.6)
        score = max(0, 1.0 - penalty - (0.2 if top3_ratio > 0.7 else 0))

        # O2 detail: full predicate distribution
        pred_distribution = [
            {"predicate": p, "count": c, "ratio": round(c / total_facts, 4)}
            for p, c in pred_counts.most_common()
        ]

        # O4 detail: unused predicates
        unused_predicates = sorted(reg.relation_ids - set(pred_counts.keys()))

        return {
            "O1_overlapping_predicates": overlapping,
            "O2_top3_predicate_ratio": round(top3_ratio, 4),
            "O2_predicate_distribution": pred_distribution,
            "O3_concentrated_nodes": len(concentrated),
            "O3_concentrated_nodes_detail": concentrated[:30],
            "O4_predicate_utilization": round(utilization, 4),
            "O4_unused_predicates": unused_predicates,
            "O5_similar_node_pairs": similar_pairs,
            "score": round(score, 4),
            "issues": issues,
        }

    # ── L: Cross-Layer Connectivity ───────────────────────────────

    def _cross_layer(self) -> dict:
        g = self._graph
        issues = []

        layer_pairs = [
            ("concept", "mechanism", "OntologyNode", "MechanismNode"),
            ("mechanism", "method", "MechanismNode", "MethodNode"),
            ("method", "condition", "MethodNode", "ConditionRuleNode"),
            ("condition", "scenario", "ConditionRuleNode", "ScenarioPatternNode"),
            ("method", "scenario", "MethodNode", "ScenarioPatternNode"),
        ]

        coverage = {}
        for src_name, tgt_name, src_label, tgt_label in layer_pairs:
            total_rows = g.read(
                f"MATCH (n:{src_label}) WHERE n.lifecycle_state='active' RETURN count(n) AS cnt"
            )
            total = total_rows[0]["cnt"] if total_rows else 0

            connected_rows = g.read(
                f"""MATCH (n:{src_label})-[r WHERE r.predicate IS NOT NULL]-(m:{tgt_label})
                    WHERE n.lifecycle_state='active'
                    RETURN count(DISTINCT n) AS cnt"""
            )
            connected = connected_rows[0]["cnt"] if connected_rows else 0
            cov = connected / max(total, 1)
            key = f"{src_name}→{tgt_name}"
            coverage[key] = round(cov, 4)

            if cov < 0.1:
                issues.append({"type": "layer_gap", "pair": key, "coverage": round(cov, 4),
                               "suggestion": f"Less than 10% of {src_name} connected to {tgt_name}"})

        # L2: Short-circuit rate (concept→scenario direct, bypassing middle layers)
        direct_rows = g.read(
            """MATCH (c:OntologyNode)-[r WHERE r.predicate IS NOT NULL]-(s:ScenarioPatternNode)
               WHERE c.lifecycle_state='active'
               RETURN count(r) AS cnt"""
        )
        direct = direct_rows[0]["cnt"] if direct_rows else 0
        total_cross = sum(1 for v in coverage.values() if v > 0)
        # Simplified: just report the count
        if direct > 0:
            log.debug("L2: %d direct concept→scenario edges", direct)

        # L3: Complete paths (concept→mech→method→cond→scenario)
        path_rows = g.read(
            """MATCH (c:OntologyNode)-[r1 WHERE r1.predicate IS NOT NULL]-(m:MechanismNode)
                     -[r2 WHERE r2.predicate IS NOT NULL]-(mt:MethodNode)
                     -[r3 WHERE r3.predicate IS NOT NULL]-(cn:ConditionRuleNode)
                     -[r4 WHERE r4.predicate IS NOT NULL]-(s:ScenarioPatternNode)
               WHERE c.lifecycle_state='active'
               RETURN count(DISTINCT c) AS paths"""
        )
        complete_paths = path_rows[0]["paths"] if path_rows else 0

        avg_cov = sum(coverage.values()) / max(len(coverage), 1)
        score = min(1.0, avg_cov * 2)  # scale: 0.5 coverage → score 1.0

        return {
            "L1_coverage": coverage,
            "L2_direct_concept_scenario": direct,
            "L3_complete_paths": complete_paths,
            "score": round(score, 4),
            "issues": issues,
        }

    # ── D: Discoverability ────────────────────────────────────────

    def _discoverability(self) -> dict:
        g = self._graph
        s = self._store
        issues = []

        # D1: Alias coverage
        total_rows = g.read(
            "MATCH (n:OntologyNode) WHERE n.lifecycle_state='active' RETURN count(n) AS cnt"
        )
        total_nodes = total_rows[0]["cnt"] if total_rows else 0

        aliased_rows = g.read(
            """MATCH (:Alias)-[:ALIAS_OF]->(n:OntologyNode)
               WHERE n.lifecycle_state='active'
               RETURN count(DISTINCT n) AS cnt"""
        )
        aliased = aliased_rows[0]["cnt"] if aliased_rows else 0
        alias_coverage = aliased / max(total_nodes, 1)
        if alias_coverage < 0.5:
            issues.append({"type": "low_alias_coverage", "value": round(alias_coverage, 4),
                           "suggestion": f"Only {aliased}/{total_nodes} nodes have aliases"})

        # D3: Relation type utilization (reuse from orthogonality)
        from src.ontology.registry import OntologyRegistry
        reg = OntologyRegistry.from_default()
        defined = len(reg.relation_ids)
        used_rows = s.fetchall("SELECT DISTINCT predicate FROM facts")
        used = len(used_rows)
        rel_util = used / max(defined, 1)
        if rel_util < 0.3:
            issues.append({"type": "low_relation_utilization", "used": used, "defined": defined,
                           "suggestion": f"Only {used}/{defined} relation types in use"})

        # D4: Tag hit rate (nodes that have been tagged in at least one segment)
        tagged_rows = s.fetchone(
            "SELECT count(DISTINCT ontology_node_id) AS cnt FROM segment_tags WHERE tag_type='canonical'"
        )
        tagged = tagged_rows["cnt"] if tagged_rows else 0
        tag_rate = tagged / max(total_nodes, 1)
        if tag_rate < 0.3:
            issues.append({"type": "low_tag_rate", "value": round(tag_rate, 4),
                           "suggestion": f"Only {tagged}/{total_nodes} nodes tagged in segments"})

        score = (alias_coverage * 0.4 + rel_util * 0.3 + tag_rate * 0.3)

        return {
            "D1_alias_coverage": round(alias_coverage, 4),
            "D3_relation_utilization": round(rel_util, 4),
            "D4_tag_hit_rate": round(tag_rate, 4),
            "score": round(score, 4),
            "issues": issues,
        }

    # ── S: Structural Health ──────────────────────────────────────

    # Neo4j label for each ontology layer
    _LAYER_LABELS = {
        "concept": "OntologyNode",
        "mechanism": "MechanismNode",
        "method": "MethodNode",
        "condition": "ConditionRuleNode",
        "scenario": "ScenarioPatternNode",
    }

    def _structural(self) -> dict:
        g = self._graph
        issues = []

        # ── Per-layer connectivity metrics ───────────────────────────
        layer_connectivity = {}
        for layer_name, label in self._LAYER_LABELS.items():
            lm = self._layer_connectivity(g, label, layer_name)
            layer_connectivity[layer_name] = lm
            for iss in lm.get("issues", []):
                issues.append(iss)

        # ── Global graph (all layers combined) ───────────────────────
        global_metrics = self._layer_connectivity(g, None, "global")
        for iss in global_metrics.get("issues", []):
            issues.append(iss)

        # ── S2: Dependency cycles ────────────────────────────────────
        cycle_rows = g.read(
            """MATCH path = (a:OntologyNode)-[:DEPENDS_ON*2..5]->(a)
               RETURN count(path) AS cycles LIMIT 1"""
        )
        cycles = cycle_rows[0]["cycles"] if cycle_rows else 0
        cycle_detail = []
        if cycles > 0:
            issues.append({"type": "dependency_cycle", "count": cycles,
                           "suggestion": "Dependency graph contains cycles — logic error"})
            try:
                cycle_detail_rows = g.read(
                    """MATCH path = (a:OntologyNode)-[:DEPENDS_ON*2..5]->(a)
                       RETURN [n IN nodes(path) | n.node_id] AS cycle_nodes LIMIT 5"""
                )
                cycle_detail = [r["cycle_nodes"] for r in cycle_detail_rows]
            except Exception:
                pass

        # ── Top degree nodes ─────────────────────────────────────────
        top_degree_rows = g.read(
            """MATCH (n) WHERE n.lifecycle_state='active'
               AND (n:OntologyNode OR n:MechanismNode OR n:MethodNode
                    OR n:ConditionRuleNode OR n:ScenarioPatternNode)
               OPTIONAL MATCH (n)-[r WHERE r.predicate IS NOT NULL]-()
               WITH n, count(r) AS degree
               RETURN n.node_id AS node_id, n.canonical_name AS name, degree
               ORDER BY degree DESC LIMIT 15"""
        )
        top_degree = [{"node_id": r["node_id"], "name": r["name"], "degree": r["degree"]}
                      for r in top_degree_rows]

        # ── Score ────────────────────────────────────────────────────
        score = 0.8
        if cycles > 0:
            score -= 0.3
        global_lcc = global_metrics.get("lcc_ratio", 0)
        if global_lcc < 0.5:
            score -= 0.2
        elif global_metrics.get("disconnected_nodes", 0) / max(global_metrics.get("total_nodes", 1), 1) > 0.4:
            score -= 0.2
        score = max(0, round(score, 4))

        return {
            "layer_connectivity": layer_connectivity,
            "global_connectivity": global_metrics,
            "S1_top_degree": top_degree,
            "S2_dependency_cycles": cycles,
            "S2_cycle_detail": cycle_detail,
            "score": round(score, 4),
            "issues": issues,
        }

    def _layer_connectivity(self, g, label: str | None, layer_name: str) -> dict:
        """Compute 4 connectivity metrics for one layer (or all layers if label=None).

        Returns: lcc_ratio, avg_reachability, density, fiedler_value + diagnostics.
        """
        if label is not None:
            node_match = f"(n:{label})"
            edge_match = f"(a:{label})-[r WHERE r.predicate IS NOT NULL]-(b:{label})"
            dir_edge_match = f"(a:{label})-[r WHERE r.predicate IS NOT NULL]->(b)"
        else:
            all_labels = "|".join(self._LAYER_LABELS.values())
            node_match = f"(n:{all_labels})"
            edge_match = (
                "(a)-[r WHERE r.predicate IS NOT NULL]-(b) "
                "WHERE (a:OntologyNode OR a:MechanismNode OR a:MethodNode "
                "OR a:ConditionRuleNode OR a:ScenarioPatternNode) "
                "AND (b:OntologyNode OR b:MechanismNode OR b:MethodNode "
                "OR b:ConditionRuleNode OR b:ScenarioPatternNode)"
            )
            dir_edge_match = (
                "(a)-[r WHERE r.predicate IS NOT NULL]->(b) "
                "WHERE (a:OntologyNode OR a:MechanismNode OR a:MethodNode "
                "OR a:ConditionRuleNode OR a:ScenarioPatternNode) "
                "AND a.lifecycle_state='active'"
            )

        # Load nodes
        node_rows = g.read(
            f"MATCH {node_match} WHERE n.lifecycle_state='active' RETURN n.node_id AS nid"
        )
        all_ids = [r["nid"] for r in node_rows if r["nid"]]
        total = len(all_ids)

        if total < 2:
            return {"total_nodes": total, "lcc_ratio": 1.0 if total == 1 else 0,
                    "avg_reachability": 1.0 if total == 1 else 0,
                    "density": 0, "fiedler_value": None, "issues": []}

        # Load undirected adjacency (within this layer/scope)
        if label is not None:
            edge_rows = g.read(
                f"""MATCH {edge_match}
                    WHERE a.lifecycle_state='active' AND b.lifecycle_state='active'
                    RETURN DISTINCT a.node_id AS src, b.node_id AS dst"""
            )
        else:
            edge_rows = g.read(
                f"""MATCH {edge_match}
                    AND a.lifecycle_state='active' AND b.lifecycle_state='active'
                    RETURN DISTINCT a.node_id AS src, b.node_id AS dst"""
            )
        id_set = set(all_ids)
        adj: dict[str, set[str]] = {nid: set() for nid in all_ids}
        for row in edge_rows:
            s, d = row["src"], row["dst"]
            if s in id_set and d in id_set:
                adj[s].add(d)
                adj[d].add(s)

        # 1) LCC ratio
        components = self._find_connected_components(all_ids, adj)
        comp_sizes = sorted((len(c) for c in components), reverse=True)
        lcc_size = comp_sizes[0] if comp_sizes else 0
        lcc_ratio = lcc_size / total

        # 2) Average reachability
        avg_reach = self._avg_reachability(all_ids, adj)

        # 3) Density (directed edges / V*(V-1))
        if label is not None:
            dir_rows = g.read(
                f"""MATCH {dir_edge_match}
                    WHERE a.lifecycle_state='active'
                    RETURN count(r) AS cnt"""
            )
        else:
            dir_rows = g.read(
                f"MATCH {dir_edge_match} RETURN count(r) AS cnt"
            )
        edge_count = dir_rows[0]["cnt"] if dir_rows else 0
        density = edge_count / (total * (total - 1))

        # 4) Fiedler value
        fiedler = self._fiedler_value(all_ids, adj)

        # Issues
        layer_issues = []
        if lcc_ratio < 0.5:
            layer_issues.append({
                "type": "fragmented_layer", "layer": layer_name,
                "lcc_ratio": round(lcc_ratio, 4), "components": len(components),
                "suggestion": f"{layer_name}: largest component covers <50% of layer nodes",
            })
        if fiedler is not None and fiedler < 0.01 and lcc_ratio > 0.5:
            layer_issues.append({
                "type": "low_algebraic_connectivity", "layer": layer_name,
                "fiedler": round(fiedler, 6),
                "suggestion": f"{layer_name}: near-zero Fiedler value; layer barely connected",
            })

        result = {
            "total_nodes": total,
            "lcc_size": lcc_size,
            "lcc_ratio": round(lcc_ratio, 4),
            "num_components": len(components),
            "component_sizes": comp_sizes[:10],
            "avg_reachability": round(avg_reach, 4),
            "density": round(density, 6),
            "edge_count": edge_count,
            "issues": layer_issues,
        }
        if fiedler is not None:
            result["fiedler_value"] = round(fiedler, 6)
        return result

    # ── Connectivity helpers ─────────────────────────────────────

    @staticmethod
    def _find_connected_components(node_ids: list[str],
                                   adj: dict[str, set[str]]) -> list[set[str]]:
        """BFS-based connected component discovery."""
        visited: set[str] = set()
        components: list[set[str]] = []
        for nid in node_ids:
            if nid in visited:
                continue
            component: set[str] = set()
            queue = [nid]
            while queue:
                current = queue.pop()
                if current in visited:
                    continue
                visited.add(current)
                component.add(current)
                for neighbor in adj.get(current, ()):
                    if neighbor not in visited:
                        queue.append(neighbor)
            components.append(component)
        return components

    @staticmethod
    def _avg_reachability(node_ids: list[str], adj: dict[str, set[str]]) -> float:
        """Average fraction of graph reachable from each node via BFS.

        Samples 80 nodes when graph > 200 to keep runtime bounded.
        """
        n = len(node_ids)
        if n <= 1:
            return 1.0

        import random
        sample = node_ids if n <= 200 else random.sample(node_ids, 80)
        reach_sum = 0.0

        for start in sample:
            visited: set[str] = set()
            queue = [start]
            while queue:
                cur = queue.pop()
                if cur in visited:
                    continue
                visited.add(cur)
                for nb in adj.get(cur, ()):
                    if nb not in visited:
                        queue.append(nb)
            reach_sum += (len(visited) - 1) / (n - 1)

        return reach_sum / len(sample)

    @staticmethod
    def _fiedler_value(node_ids: list[str], adj: dict[str, set[str]]):
        """Algebraic connectivity: 2nd smallest eigenvalue of graph Laplacian.

        Returns None if numpy unavailable or graph < 3 nodes.
        Uses sparse eigsh (scipy) when > 300 nodes; dense eigvalsh otherwise.
        """
        n = len(node_ids)
        if n < 3:
            return None
        try:
            import numpy as np
        except ImportError:
            log.debug("numpy unavailable, skipping Fiedler value")
            return None

        idx = {nid: i for i, nid in enumerate(node_ids)}

        if n <= 300:
            laplacian = np.zeros((n, n), dtype=np.float64)
            for nid, neighbors in adj.items():
                i = idx.get(nid)
                if i is None:
                    continue
                laplacian[i, i] = len(neighbors)
                for nb in neighbors:
                    j = idx.get(nb)
                    if j is not None:
                        laplacian[i, j] = -1.0
            eigenvalues = np.linalg.eigvalsh(laplacian)
            return float(eigenvalues[1]) if len(eigenvalues) > 1 else 0.0
        else:
            try:
                from scipy.sparse import lil_matrix
                from scipy.sparse.linalg import eigsh
            except ImportError:
                log.debug("scipy unavailable, skipping Fiedler value for large graph")
                return None
            lap = lil_matrix((n, n), dtype=np.float64)
            for nid, neighbors in adj.items():
                i = idx.get(nid)
                if i is None:
                    continue
                lap[i, i] = len(neighbors)
                for nb in neighbors:
                    j = idx.get(nb)
                    if j is not None:
                        lap[i, j] = -1.0
            eigenvalues = eigsh(lap.tocsr(), k=2, which="SM", return_eigenvectors=False)
            eigenvalues.sort()
            return float(eigenvalues[1]) if len(eigenvalues) > 1 else 0.0

    # ── Node Similarity Detection ────────────────────────────────

    def _detect_similar_nodes(self) -> list[dict]:
        """Detect node pairs with high semantic similarity.

        Three complementary signals:
        - Neighborhood overlap: Jaccard of connected-neighbor sets in Neo4j
        - Tag co-occurrence: fraction of segments where both nodes appear
        - Embedding cosine: semantic similarity of node canonical names

        Weights: 0.35 neighbor + 0.35 tag + 0.30 embedding (degrades to 0.5/0.5 if no embedding).
        """
        g = self._graph
        s = self._store

        # Step 1: Build neighbor sets from Neo4j
        neighbor_rows = g.read(
            """MATCH (n:OntologyNode)-[r WHERE r.predicate IS NOT NULL]-(m)
               WHERE n.lifecycle_state = 'active'
               RETURN n.node_id AS node, m.node_id AS neighbor"""
        )
        neighbors: dict[str, set] = {}
        for row in neighbor_rows:
            nid = row["node"]
            mid = row["neighbor"]
            if nid and mid:
                neighbors.setdefault(nid, set()).add(mid)

        # Step 2: Build tag co-occurrence from PG (segment_tags)
        tag_rows = s.fetchall(
            """SELECT ontology_node_id, segment_id
               FROM segment_tags
               WHERE tag_type = 'canonical' AND ontology_node_id IS NOT NULL"""
        )
        node_segments: dict[str, set] = {}
        for row in tag_rows:
            nid = row["ontology_node_id"]
            sid = str(row["segment_id"])
            node_segments.setdefault(nid, set()).add(sid)

        # Step 3: Build embedding similarity matrix (if available)
        candidates = set(neighbors.keys()) | set(node_segments.keys())
        node_list = sorted(candidates)
        emb_sim = self._compute_node_embeddings(node_list)
        has_embedding = emb_sim is not None

        # Step 4: Compare all pairs
        similar: list[dict] = []

        for i, n1 in enumerate(node_list):
            for n2 in node_list[i + 1:]:
                j = node_list.index(n2)

                # Neighborhood Jaccard
                nb1 = neighbors.get(n1, set()) - {n2}
                nb2 = neighbors.get(n2, set()) - {n1}
                nb_union = nb1 | nb2
                nb_jaccard = len(nb1 & nb2) / len(nb_union) if nb_union else 0

                # Tag co-occurrence Jaccard
                seg1 = node_segments.get(n1, set())
                seg2 = node_segments.get(n2, set())
                seg_union = seg1 | seg2
                tag_jaccard = len(seg1 & seg2) / len(seg_union) if seg_union else 0

                # Embedding cosine
                if has_embedding:
                    emb_cos = float(emb_sim[i][j])
                    combined = 0.35 * nb_jaccard + 0.35 * tag_jaccard + 0.30 * emb_cos
                else:
                    emb_cos = 0.0
                    combined = 0.5 * nb_jaccard + 0.5 * tag_jaccard

                if combined >= 0.3:
                    entry = {
                        "node_a": n1,
                        "node_b": n2,
                        "neighbor_jaccard": round(nb_jaccard, 4),
                        "tag_cooccurrence": round(tag_jaccard, 4),
                        "combined_score": round(combined, 4),
                    }
                    if has_embedding:
                        entry["embedding_cosine"] = round(emb_cos, 4)
                    similar.append(entry)

        similar.sort(key=lambda x: -x["combined_score"])
        log.info("Node similarity: %d pairs above threshold (checked %d nodes, embedding=%s)",
                 len(similar), len(node_list), has_embedding)
        return similar[:50]

    def _compute_node_embeddings(self, node_ids: list[str]):
        """Compute pairwise embedding similarity matrix for node canonical names."""
        from src.config.settings import settings
        if not getattr(settings, "EMBEDDING_ENABLED", False):
            return None
        try:
            from src.utils.embedding import get_embeddings
            import numpy as np
            g = self._graph
            # Get canonical names for node_ids
            texts = []
            for nid in node_ids:
                rows = g.read(
                    "MATCH (n {node_id: $nid}) RETURN n.canonical_name AS name",
                    nid=nid,
                )
                name = rows[0]["name"] if rows else nid
                texts.append((name or nid).lower())

            vecs = get_embeddings(texts)
            if vecs is None:
                return None
            emb = np.array(vecs)
            return np.dot(emb, emb.T)  # pairwise cosine (already normalized)
        except Exception as exc:
            log.debug("Node embedding computation failed: %s", exc)
            return None

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _gini(values: list[int | float]) -> float:
        """Compute Gini coefficient for a list of values."""
        if not values:
            return 0
        n = len(values)
        sorted_vals = sorted(values)
        total = sum(sorted_vals) or 1
        cumsum = 0
        gini_sum = 0
        for i, v in enumerate(sorted_vals):
            cumsum += v
            gini_sum += (2 * (i + 1) - n - 1) * v
        return gini_sum / (n * total)