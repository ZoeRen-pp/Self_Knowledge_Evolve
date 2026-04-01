"""TelecomEvolutionGate — EvolutionGate backed by evolution_gate() logic."""

from __future__ import annotations

from pathlib import Path

import yaml

from semcore.core.types import EvolutionCandidate
from semcore.governance.base import EvolutionGate, GateResult
from semcore.providers.base import RelationalStore

_POLICY_PATH = Path("ontology/governance/evolution_policy.yaml")


def _load_policy() -> dict:
    if _POLICY_PATH.exists():
        return yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8")) or {}
    return {}


class TelecomEvolutionGate(EvolutionGate):
    GATES = [
        "source_count",
        "source_diversity",
        "temporal_stability",
        "structural_fit",
        "composite_score",
        "synonym_risk",
    ]

    def evaluate(self, candidate: EvolutionCandidate, store: RelationalStore) -> GateResult:
        policy    = _load_policy()
        admission = policy.get("candidate_admission", {})

        thresholds = {
            "min_source_count":       int(admission.get("min_source_count", 3)),
            "min_source_diversity":   float(admission.get("min_source_diversity", 0.6)),
            "min_temporal_stability": float(admission.get("min_temporal_stability", 0.7)),
            "min_structural_fit":     float(admission.get("min_structural_fit", 0.65)),
            "min_composite_score":    float(admission.get("min_composite_score", 0.65)),
            "synonym_risk_max":       float(admission.get("synonym_risk_max", 0.4)),
        }

        gate_scores = {
            "source_count":      float(candidate.source_count),
            "source_diversity":  candidate.source_diversity_score,
            "temporal_stability":candidate.temporal_stability_score,
            "structural_fit":    candidate.structural_fit_score,
            "composite_score":   candidate.composite_score,
            "synonym_risk":      candidate.synonym_risk_score,
        }

        blocking: list[str] = []
        if candidate.source_count             < thresholds["min_source_count"]:
            blocking.append(f"source_count {candidate.source_count} < {thresholds['min_source_count']}")
        if candidate.source_diversity_score   < thresholds["min_source_diversity"]:
            blocking.append(f"source_diversity {candidate.source_diversity_score:.2f} < {thresholds['min_source_diversity']}")
        if candidate.temporal_stability_score < thresholds["min_temporal_stability"]:
            blocking.append(f"temporal_stability {candidate.temporal_stability_score:.2f} < {thresholds['min_temporal_stability']}")
        if candidate.structural_fit_score     < thresholds["min_structural_fit"]:
            blocking.append(f"structural_fit {candidate.structural_fit_score:.2f} < {thresholds['min_structural_fit']}")
        if candidate.composite_score          < thresholds["min_composite_score"]:
            blocking.append(f"composite_score {candidate.composite_score:.2f} < {thresholds['min_composite_score']}")
        if candidate.synonym_risk_score       > thresholds["synonym_risk_max"]:
            blocking.append(f"synonym_risk {candidate.synonym_risk_score:.2f} > {thresholds['synonym_risk_max']}")

        passed = len(blocking) == 0
        if passed:
            store.execute(
                "UPDATE governance.evolution_candidates SET review_status='pending_review' WHERE candidate_id=%s",
                (candidate.candidate_id,),
            )

        return GateResult(
            passed=passed,
            gate_scores=gate_scores,
            reason="; ".join(blocking) if blocking else "all gates passed",
        )