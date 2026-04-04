"""StatsCollector — compute all system metrics by reading PG + Neo4j directly."""

from __future__ import annotations

import logging
import time
from typing import Any

log = logging.getLogger(__name__)


class StatsCollector:
    """Reads PG and Neo4j to produce a full metrics snapshot."""

    def __init__(self, store, graph, crawler_store=None):
        self._store = store
        self._graph = graph
        self._crawler_store = crawler_store or store

    def collect_all(self) -> dict[str, Any]:
        """Compute all 7 metric categories. Returns a JSON-serializable dict."""
        t0 = time.monotonic()
        snapshot = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "knowledge": self._knowledge_scale(),
            "quality": self._knowledge_quality(),
            "sources": self._source_distribution(),
            "evolution": self._evolution_status(),
            "pipeline": self._pipeline_health(),
            "graph_health": self._graph_health(),
            "ontology_health": self._ontology_health(),
        }
        elapsed = time.monotonic() - t0
        snapshot["collection_time_ms"] = round(elapsed * 1000)
        log.info("Stats collected in %.1fs", elapsed)
        return snapshot

    # ── 1. Knowledge scale ────────────────────────────────────────

    def _knowledge_scale(self) -> dict:
        s = self._store
        docs = self._count_grouped(s, "documents", "status")
        segs = self._count_grouped(s, "segments", "lifecycle_state")
        facts = self._count_grouped(s, "facts", "lifecycle_state")

        neo4j_nodes = self._neo4j_count("MATCH (n) RETURN count(n) AS cnt")
        neo4j_rels = self._neo4j_count("MATCH ()-[r]->() RETURN count(r_fact) AS cnt")

        return {
            "documents": docs,
            "segments": segs,
            "facts": facts,
            "evidence_total": self._count(s, "evidence"),
            "rst_relations_total": self._count(s, "t_rst_relation"),
            "neo4j_nodes": neo4j_nodes,
            "neo4j_relationships": neo4j_rels,
        }

    # ── 2. Knowledge quality ──────────────────────────────────────

    def _knowledge_quality(self) -> dict:
        s = self._store
        g = self._graph

        total_facts = self._count(s, "facts", "lifecycle_state='active'")

        # Ontology coverage: nodes referenced by at least one fact
        total_nodes = self._neo4j_count(
            "MATCH (n:OntologyNode) WHERE n.lifecycle_state='active' RETURN count(n) AS cnt"
        )
        covered_nodes = self._neo4j_count(
            """MATCH (n:OntologyNode)-[r_fact WHERE r_fact.predicate IS NOT NULL]-()
               WHERE n.lifecycle_state='active'
               RETURN count(DISTINCT n) AS cnt"""
        )
        coverage = covered_nodes / max(total_nodes, 1)

        # Average confidence
        avg_row = s.fetchone(
            "SELECT AVG(confidence) AS avg_conf FROM facts WHERE lifecycle_state='active'"
        )
        avg_conf = float(avg_row["avg_conf"] or 0) if avg_row else 0

        # Low confidence ratio
        low_conf = self._count(s, "facts", "lifecycle_state='active' AND confidence < 0.5")
        low_ratio = low_conf / max(total_facts, 1)

        # Conflict ratio
        conflicted = self._count(s, "facts", "lifecycle_state='conflicted'")
        conflict_ratio = conflicted / max(total_facts + conflicted, 1)

        # Segment dedup ratio
        total_segs = self._count(s, "segments")
        superseded = self._count(s, "segments", "lifecycle_state='superseded'")
        dedup_ratio = superseded / max(total_segs, 1)

        # Single evidence weak ratio
        weak_row = s.fetchone(
            """SELECT count(*) AS cnt FROM (
                SELECT f.fact_id FROM facts f
                JOIN evidence e ON f.fact_id = e.fact_id
                WHERE f.lifecycle_state='active'
                GROUP BY f.fact_id
                HAVING count(e.evidence_id) = 1 AND max(e.source_rank) IN ('B','C')
            ) sub"""
        )
        weak_count = weak_row["cnt"] if weak_row else 0
        weak_ratio = weak_count / max(total_facts, 1)

        return {
            "ontology_coverage": round(coverage, 4),
            "covered_nodes": covered_nodes,
            "total_nodes": total_nodes,
            "avg_fact_confidence": round(avg_conf, 4),
            "low_confidence_ratio": round(low_ratio, 4),
            "conflict_ratio": round(conflict_ratio, 4),
            "segment_dedup_ratio": round(dedup_ratio, 4),
            "single_evidence_weak_ratio": round(weak_ratio, 4),
        }

    # ── 3. Source distribution ────────────────────────────────────

    def _source_distribution(self) -> dict:
        s = self._store
        by_rank = self._count_grouped(s, "documents", "source_rank", where="status='indexed'")
        by_site = self._count_grouped(s, "documents", "site_key", where="status='indexed'")
        by_method = self._count_grouped(s, "evidence", "extraction_method")
        return {"by_rank": by_rank, "by_site": by_site, "by_method": by_method}

    # ── 4. Evolution status ───────────────────────────────────────

    def _evolution_status(self) -> dict:
        s = self._store
        by_status = self._count_grouped(s, "governance.evolution_candidates", "review_status")
        by_type = self._count_grouped(s, "governance.evolution_candidates", "candidate_type")
        return {
            "candidates_by_status": by_status,
            "candidates_by_type": by_type,
        }

    # ── 5. Pipeline health ────────────────────────────────────────

    def _pipeline_health(self) -> dict:
        s = self._store
        backlog = self._count(s, "documents", "status='raw'")
        failed = self._count(s, "documents", "status='failed'")

        recent_row = s.fetchone(
            """SELECT count(*) AS cnt FROM documents
               WHERE status='indexed' AND updated_at > NOW() - INTERVAL '24 hours'"""
        )
        processed_24h = recent_row["cnt"] if recent_row else 0

        return {
            "backlog": backlog,
            "failed_count": failed,
            "processed_24h": processed_24h,
        }

    # ── 6. Graph health (summary only — detail via operators) ─────

    def _graph_health(self) -> dict:
        g = self._graph
        s = self._store

        # Isolated nodes count
        iso_rows = g.read(
            """MATCH (n:OntologyNode)
               WHERE n.lifecycle_state='active'
               OPTIONAL MATCH (n)-[r_fact WHERE r_fact.predicate IS NOT NULL]-()
               WITH n, count(r_fact) AS rc WHERE rc = 0
               RETURN count(n) AS cnt"""
        )
        isolated = iso_rows[0]["cnt"] if iso_rows else 0

        # Degree stats
        deg_rows = g.read(
            """MATCH (n:OntologyNode)
               WHERE n.lifecycle_state='active'
               OPTIONAL MATCH (n)-[r_fact WHERE r_fact.predicate IS NOT NULL]-()
               WITH n, count(r_fact) AS degree
               RETURN avg(degree) AS avg_deg, max(degree) AS max_deg"""
        )
        deg = dict(deg_rows[0]) if deg_rows else {}

        # Predicate utilization
        from src.ontology.registry import OntologyRegistry
        registry = OntologyRegistry.from_default()
        defined_count = len(registry.relation_ids)
        used_rows = s.fetchall("SELECT DISTINCT predicate FROM facts")
        used_count = len(used_rows)

        return {
            "isolated_node_count": isolated,
            "avg_degree": round(float(deg.get("avg_deg") or 0), 2),
            "max_degree": deg.get("max_deg", 0),
            "predicate_utilization": round(used_count / max(defined_count, 1), 4),
            "predicates_used": used_count,
            "predicates_defined": defined_count,
        }

    # ── 7. Ontology health (summary only) ─────────────────────────

    def _ontology_health(self) -> dict:
        g = self._graph

        # Max inheritance depth
        depth_rows = g.read(
            """MATCH path=(leaf)-[:SUBCLASS_OF*]->(root)
               WHERE NOT EXISTS { (root)-[:SUBCLASS_OF]->() }
               RETURN max(length(path)) AS max_depth"""
        )
        max_depth = depth_rows[0]["max_depth"] if depth_rows else 0

        # Branch factor
        branch_rows = g.read(
            """MATCH (child)-[:SUBCLASS_OF]->(parent)
               WITH parent, count(child) AS children
               RETURN avg(children) AS avg_branch,
                      sum(CASE WHEN children=1 THEN 1 ELSE 0 END) AS single_child,
                      count(parent) AS parent_count"""
        )
        b = dict(branch_rows[0]) if branch_rows else {}
        parent_count = b.get("parent_count", 1) or 1

        # No-alias count
        no_alias_rows = g.read(
            """MATCH (n:OntologyNode)
               WHERE n.lifecycle_state='active'
                 AND NOT EXISTS { MATCH (:Alias)-[:ALIAS_OF]->(n) }
               RETURN count(n) AS cnt"""
        )
        no_alias = no_alias_rows[0]["cnt"] if no_alias_rows else 0

        return {
            "max_inheritance_depth": max_depth,
            "avg_branch_factor": round(float(b.get("avg_branch") or 0), 2),
            "single_child_ratio": round((b.get("single_child", 0) or 0) / parent_count, 4),
            "no_alias_node_count": no_alias,
        }

    # ── Helpers ───────────────────────────────────────────────────

    @staticmethod
    def _count(store, table: str, where: str = "") -> int:
        clause = f"WHERE {where}" if where else ""
        row = store.fetchone(f"SELECT count(*) AS cnt FROM {table} {clause}")
        return row["cnt"] if row else 0

    @staticmethod
    def _count_grouped(store, table: str, group_col: str, where: str = "") -> dict:
        clause = f"WHERE {where}" if where else ""
        rows = store.fetchall(
            f"SELECT {group_col}, count(*) AS cnt FROM {table} {clause} GROUP BY {group_col}"
        )
        return {r[group_col]: r["cnt"] for r in rows}

    def _neo4j_count(self, cypher: str) -> int:
        rows = self._graph.read(cypher)
        return rows[0]["cnt"] if rows else 0