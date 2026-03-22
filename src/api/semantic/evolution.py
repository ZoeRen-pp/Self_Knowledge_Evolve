"""Evolution operators: candidate_discover, attach_score, evolution_gate."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.db.postgres import fetchall, fetchone, execute
from src.db.neo4j_client import run_query

_POLICY_PATH = Path("ontology/governance/evolution_policy.yaml")


def _load_policy() -> dict:
    if _POLICY_PATH.exists():
        return yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8")) or {}
    return {}


def candidate_discover(
    window_days: int,
    min_frequency: int = 5,
    domain: str | None = None,
    min_source_count: int = 2,
) -> dict:
    rows = fetchall(
        """
        SELECT normalized_form, COUNT(*) AS freq, MAX(last_seen_at) AS latest,
               source_count, review_status
        FROM evolution_candidates
        WHERE first_seen_at >= NOW() - INTERVAL '%s days'
          AND source_count >= %s
        GROUP BY normalized_form, source_count, review_status
        HAVING COUNT(*) >= %s
        ORDER BY freq DESC
        LIMIT 100
        """,
        (window_days, min_source_count, min_frequency),
    )
    return {
        "window_days": window_days,
        "candidates":  [dict(r) for r in rows],
        "total":       len(rows),
    }


def attach_score(
    candidate_id: str,
    candidate_parent_ids: list[str] | None = None,
) -> dict:
    candidate = fetchone(
        "SELECT * FROM evolution_candidates WHERE candidate_id=%s", (candidate_id,)
    )
    if not candidate:
        return {"error": f"Candidate '{candidate_id}' not found"}

    surface_forms = candidate.get("surface_forms") or []
    keyword_set = set(w.lower() for sf in surface_forms for w in sf.split())

    # If no parent IDs given, query top-level ontology nodes
    if not candidate_parent_ids:
        parent_rows = run_query(
            "MATCH (n:OntologyNode) WHERE n.lifecycle_state='active' RETURN n.node_id AS nid, n.canonical_name AS name, n.description AS desc LIMIT 50"
        )
        candidate_parent_ids_map = {r["nid"]: r for r in parent_rows}
    else:
        candidate_parent_ids_map = {}
        for pid in candidate_parent_ids:
            rows = run_query("MATCH (n:OntologyNode {node_id: $id}) RETURN n.node_id AS nid, n.canonical_name AS name, n.description AS desc LIMIT 1", id=pid)
            if rows:
                candidate_parent_ids_map[pid] = rows[0]

    recommendations = []
    for nid, node in candidate_parent_ids_map.items():
        node_words = set(
            w.lower() for w in (str(node.get("name") or "") + " " + str(node.get("desc") or "")).split()
        )
        overlap = len(keyword_set & node_words)
        union   = len(keyword_set | node_words)
        score   = round(overlap / union, 4) if union else 0.0
        recommendations.append({"parent_node_id": nid, "structural_fit_score": score})

    recommendations.sort(key=lambda x: -x["structural_fit_score"])
    return {
        "candidate_id":    candidate_id,
        "normalized_form": candidate.get("normalized_form"),
        "recommendations": recommendations[:5],
    }


def evolution_gate(candidate_id: str) -> dict:
    candidate = fetchone(
        "SELECT * FROM evolution_candidates WHERE candidate_id=%s", (candidate_id,)
    )
    if not candidate:
        return {"error": f"Candidate '{candidate_id}' not found", "gate_passed": False}

    policy = _load_policy()
    admission = policy.get("candidate_admission", {})

    thresholds = {
        "min_source_count":      int(admission.get("min_source_count", 3)),
        "min_source_diversity":  float(admission.get("min_source_diversity", 0.6)),
        "min_temporal_stability":float(admission.get("min_temporal_stability", 0.7)),
        "min_structural_fit":    float(admission.get("min_structural_fit", 0.65)),
        "min_composite_score":   float(admission.get("min_composite_score", 0.65)),
        "synonym_risk_max":      float(admission.get("synonym_risk_max", 0.4)),
    }

    scores = {
        "source_count":            int(candidate.get("source_count") or 0),
        "source_diversity_score":  float(candidate.get("source_diversity_score") or 0),
        "temporal_stability_score":float(candidate.get("temporal_stability_score") or 0),
        "structural_fit_score":    float(candidate.get("structural_fit_score") or 0),
        "composite_score":         float(candidate.get("composite_score") or 0),
        "synonym_risk_score":      float(candidate.get("synonym_risk_score") or 1.0),
    }

    blocking: list[str] = []
    if scores["source_count"]            < thresholds["min_source_count"]:
        blocking.append(f"source_count {scores['source_count']} < {thresholds['min_source_count']}")
    if scores["source_diversity_score"]  < thresholds["min_source_diversity"]:
        blocking.append(f"source_diversity {scores['source_diversity_score']:.2f} < {thresholds['min_source_diversity']}")
    if scores["temporal_stability_score"]< thresholds["min_temporal_stability"]:
        blocking.append(f"temporal_stability {scores['temporal_stability_score']:.2f} < {thresholds['min_temporal_stability']}")
    if scores["structural_fit_score"]    < thresholds["min_structural_fit"]:
        blocking.append(f"structural_fit {scores['structural_fit_score']:.2f} < {thresholds['min_structural_fit']}")
    if scores["composite_score"]         < thresholds["min_composite_score"]:
        blocking.append(f"composite_score {scores['composite_score']:.2f} < {thresholds['min_composite_score']}")
    if scores["synonym_risk_score"]      > thresholds["synonym_risk_max"]:
        blocking.append(f"synonym_risk {scores['synonym_risk_score']:.2f} > {thresholds['synonym_risk_max']}")

    gate_passed = len(blocking) == 0
    if gate_passed:
        execute(
            "UPDATE evolution_candidates SET review_status='pending_review' WHERE candidate_id=%s",
            (candidate_id,),
        )

    return {
        "candidate_id":    candidate_id,
        "gate_passed":     gate_passed,
        "scores":          scores,
        "blocking_reasons":blocking,
        "action":          "submit_to_review" if gate_passed else "insufficient_scores",
    }