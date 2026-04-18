"""Stage 3b: Ontology evolution — normalize, score, gate, promote candidates."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from semcore.core.context import PipelineContext
from semcore.core.types import EvolutionCandidate
from semcore.pipeline.base import Stage
from semcore.providers.base import GraphStore, RelationalStore

from src.utils.normalize import normalize_term

log = logging.getLogger(__name__)

_POLICY_PATH = Path("ontology/governance/evolution_policy.yaml")

# Source rank → numeric authority (for scoring)
_RANK_SCORE = {"S": 1.0, "A": 0.8, "B": 0.6, "C": 0.4}


def _load_policy() -> dict:
    if _POLICY_PATH.exists():
        return yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8")) or {}
    return {}


class EvolveStage(Stage):
    """Automatically normalize, score, and gate ontology candidates.

    Inserted after AlignStage (stage3) and before ExtractStage (stage4).
    Works on candidates accumulated in `evolution_candidates` table.
    """

    name = "evolve"

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        if not source_doc_id:
            return ctx
        store = app.store
        graph = app.graph
        ontology = app.ontology
        gate = app.evolution_gate

        stats = self._run(source_doc_id, store, graph, ontology, gate)
        self.set_output(ctx, stats)
        return ctx

    def _run(
        self,
        source_doc_id: str,
        store: RelationalStore,
        graph: GraphStore,
        ontology,
        gate,
    ) -> dict:
        scored = self._score_candidates(source_doc_id, store, graph, ontology)
        enriched = self._enrich_candidates(source_doc_id, store)
        promoted = self._gate_and_promote(store, graph, ontology, gate)
        log.info(
            "Evolve doc=%s: scored=%d enriched=%d promoted=%d",
            source_doc_id, scored, enriched, promoted,
        )
        return {"candidates_scored": scored, "candidates_enriched": enriched, "candidates_promoted": promoted}

    # ── Scoring ────────────────────────────────────────────────────

    def _score_candidates(
        self,
        source_doc_id: str,
        store: RelationalStore,
        graph: GraphStore,
        ontology,
    ) -> int:
        """Score candidates that were seen in this document's processing."""
        log.debug("Scoring candidates for doc=%s", source_doc_id)
        # Only score candidates touched by this doc (recently upserted)
        candidates = store.fetchall(
            """
            SELECT * FROM governance.evolution_candidates
            WHERE %s::uuid = ANY(seen_source_doc_ids)
              AND review_status = 'discovered'
              AND source_count >= 2
            """,
            (source_doc_id,),
        )
        if not candidates:
            log.debug("No scorable candidates for doc=%s", source_doc_id)
            return 0

        log.info("Scoring %d candidates for doc=%s", len(candidates), source_doc_id)
        policy = _load_policy()
        weights = policy.get("scoring_weights", {})

        for cand in candidates:
            cid = cand["candidate_id"]
            normalized = cand.get("normalized_form") or ""
            surface_forms = cand.get("surface_forms") or []

            # source_diversity: distinct site_keys from contributing docs
            doc_ids = cand.get("seen_source_doc_ids") or []
            if doc_ids:
                diversity_rows = store.fetchall(
                    "SELECT COUNT(DISTINCT site_key) as distinct_sites FROM documents "
                    "WHERE source_doc_id = ANY(%s)",
                    (doc_ids,),
                )
                distinct_sites = diversity_rows[0]["distinct_sites"] if diversity_rows else 0
            else:
                distinct_sites = 0
            source_diversity = min(1.0, distinct_sites / 3.0)

            # temporal_stability: how long in candidate pool
            first_seen = cand.get("first_seen_at")
            if first_seen and isinstance(first_seen, datetime):
                days_alive = (datetime.now(timezone.utc) - first_seen).days
            else:
                days_alive = 0
            temporal_stability = min(1.0, days_alive / 14.0)

            # structural_fit: best Jaccard overlap with existing ontology nodes
            structural_fit, best_parent = self._compute_structural_fit(
                surface_forms, normalized, graph
            )

            # synonym_risk: overlap with existing aliases
            synonym_risk = self._compute_synonym_risk(normalized, surface_forms, ontology)

            # source_authority: best source rank from contributing documents
            if doc_ids:
                rank_rows = store.fetchall(
                    "SELECT source_rank FROM documents WHERE source_doc_id = ANY(%s)",
                    (doc_ids,),
                )
                best_rank = max(
                    (_RANK_SCORE.get(r.get("source_rank", "C"), 0.4) for r in rank_rows),
                    default=0.4,
                )
            else:
                best_rank = 0.4

            # composite_score
            w = weights
            composite = (
                w.get("source_authority", 0.25) * best_rank
                + w.get("source_diversity", 0.20) * source_diversity
                + w.get("temporal_stability", 0.20) * temporal_stability
                + w.get("structural_fit", 0.20) * structural_fit
                + w.get("retrieval_gain", 0.10) * 0.5  # placeholder
                - w.get("synonym_risk_penalty", 0.05) * synonym_risk
            )
            composite = round(max(0.0, min(1.0, composite)), 4)

            log.debug(
                "  candidate=%s form=%s diversity=%.2f stability=%.2f fit=%.2f "
                "synonym_risk=%.2f composite=%.4f parent=%s",
                str(cid)[:12], normalized, source_diversity, temporal_stability,
                structural_fit, synonym_risk, composite, best_parent,
            )

            # Persist scores
            store.execute(
                """
                UPDATE governance.evolution_candidates SET
                    source_diversity_score = %s,
                    temporal_stability_score = %s,
                    structural_fit_score = %s,
                    synonym_risk_score = %s,
                    composite_score = %s,
                    candidate_parent_id = %s
                WHERE candidate_id = %s
                """,
                (
                    round(source_diversity, 4),
                    round(temporal_stability, 4),
                    round(structural_fit, 4),
                    round(synonym_risk, 4),
                    composite,
                    best_parent,
                    cid,
                ),
            )

        return len(candidates)

    def _compute_structural_fit(
        self, surface_forms: list[str], normalized: str, graph: GraphStore
    ) -> tuple[float, str | None]:
        """Compute Jaccard word-overlap with existing ontology nodes. Return (score, best_parent_id)."""
        keyword_set = set(w.lower() for sf in surface_forms for w in sf.split())
        if not keyword_set:
            return 0.0, None

        # Query a sample of ontology nodes for comparison
        rows = graph.read(
            "MATCH (n:OntologyNode) WHERE n.lifecycle_state = 'active' "
            "RETURN n.node_id AS nid, n.canonical_name AS name, "
            "n.description AS desc LIMIT 80"
        )

        best_score = 0.0
        best_parent = None
        for row in rows:
            node_words = set(
                w.lower() for w in (
                    str(row.get("name") or "") + " " + str(row.get("desc") or "")
                ).split()
            )
            intersection = len(keyword_set & node_words)
            union = len(keyword_set | node_words)
            if union == 0:
                continue
            score = intersection / union
            if score > best_score:
                best_score = score
                best_parent = row["nid"]

        return round(best_score, 4), best_parent

    @staticmethod
    def _compute_synonym_risk(
        normalized: str, surface_forms: list[str], ontology
    ) -> float:
        """Check if candidate overlaps with existing aliases (high = likely synonym)."""
        if not hasattr(ontology, "alias_map"):
            return 0.0
        alias_map = ontology.alias_map
        best_overlap = 0.0
        for sf in surface_forms:
            sf_lower = sf.lower()
            for existing_alias in alias_map:
                # Check substring containment both ways
                if sf_lower in existing_alias or existing_alias in sf_lower:
                    # Length ratio as similarity proxy
                    ratio = min(len(sf_lower), len(existing_alias)) / max(
                        len(sf_lower), len(existing_alias), 1
                    )
                    best_overlap = max(best_overlap, ratio)
        return round(best_overlap, 4)

    # ── Gate & Promote ─────────────────────────────────────────────

    def _gate_and_promote(
        self,
        store: RelationalStore,
        graph: GraphStore,
        ontology,
        gate,
    ) -> int:
        """Run 6-gate evaluation; auto-accept high-confidence candidates."""
        candidates = store.fetchall(
            """
            SELECT * FROM governance.evolution_candidates
            WHERE review_status = 'discovered'
              AND composite_score > 0
              AND source_count >= 2
            """,
        )
        if not candidates:
            return 0

        policy = _load_policy()
        auto_threshold = float(
            policy.get("candidate_admission", {}).get("auto_accept_threshold", 0.85)
        )
        min_pool_days = int(
            policy.get("anti_drift", {}).get("min_days_in_candidate_pool", 7)
        )

        promoted = 0
        for cand in candidates:
            # Build EvolutionCandidate dataclass for the gate
            ec = EvolutionCandidate(
                candidate_id=str(cand["candidate_id"]),
                surface_forms=cand.get("surface_forms") or [],
                normalized_form=cand.get("normalized_form") or "",
                candidate_parent_id=cand.get("candidate_parent_id") or "",
                source_count=int(cand.get("source_count") or 0),
                source_diversity_score=float(cand.get("source_diversity_score") or 0),
                temporal_stability_score=float(cand.get("temporal_stability_score") or 0),
                structural_fit_score=float(cand.get("structural_fit_score") or 0),
                composite_score=float(cand.get("composite_score") or 0),
                synonym_risk_score=float(cand.get("synonym_risk_score") or 0),
            )

            result = gate.evaluate(ec, store)
            if not result.passed:
                continue

            # Check temporal constraint
            first_seen = cand.get("first_seen_at")
            if first_seen and isinstance(first_seen, datetime):
                days_in_pool = (datetime.now(timezone.utc) - first_seen).days
            else:
                days_in_pool = 0
            if days_in_pool < min_pool_days:
                log.debug(
                    "Candidate %s passed gate but only %d/%d days in pool",
                    ec.normalized_form, days_in_pool, min_pool_days,
                )
                continue

            # Gate passed + pool time met
            if ec.composite_score >= auto_threshold and ec.candidate_parent_id:
                self._auto_accept(cand, graph, ontology)
                store.execute(
                    "UPDATE governance.evolution_candidates SET review_status='auto_accepted' "
                    "WHERE candidate_id=%s",
                    (cand["candidate_id"],),
                )
                log.info(
                    "Auto-accepted candidate: %s (parent=%s, score=%.2f)",
                    ec.normalized_form, ec.candidate_parent_id, ec.composite_score,
                )
                promoted += 1
            else:
                store.execute(
                    "UPDATE governance.evolution_candidates SET review_status='pending_review' "
                    "WHERE candidate_id=%s",
                    (cand["candidate_id"],),
                )
                log.info(
                    "Candidate %s passed gate → pending_review (score=%.2f)",
                    ec.normalized_form, ec.composite_score,
                )

        return promoted

    @staticmethod
    def _auto_accept(cand: dict, graph: GraphStore, ontology) -> None:
        """Create a new OntologyNode in Neo4j with lifecycle_state='candidate'."""
        normalized = cand.get("normalized_form") or ""
        parent_id = cand.get("candidate_parent_id") or ""
        surface_forms = cand.get("surface_forms") or []
        # Generate a node_id from the normalized form
        node_id = "EVOLVED." + re.sub(r"[^a-z0-9]", "_", normalized).upper()
        display_name = surface_forms[0] if surface_forms else normalized

        graph.write(
            """
            MERGE (n:OntologyNode {node_id: $node_id})
            SET n.canonical_name = $name,
                n.lifecycle_state = 'candidate',
                n.maturity_level = 'evolved',
                n.auto_accepted = true,
                n.source_count = $source_count,
                n.composite_score = $composite_score
            """,
            node_id=node_id,
            name=display_name,
            source_count=int(cand.get("source_count") or 0),
            composite_score=float(cand.get("composite_score") or 0),
        )

        if parent_id:
            graph.write(
                """
                MATCH (child:OntologyNode {node_id: $child_id})
                MATCH (parent:OntologyNode {node_id: $parent_id})
                MERGE (child)-[:SUBCLASS_OF]->(parent)
                """,
                child_id=node_id,
                parent_id=parent_id,
            )

        # Update in-memory alias map for immediate visibility
        if hasattr(ontology, "alias_map"):
            for sf in surface_forms:
                ontology.alias_map[sf.lower()] = node_id

    # ── Enrichment (description + aliases via LLM) ─────────────────

    def _enrich_candidates(self, source_doc_id: str, store) -> int:
        """Generate bilingual description + suggested aliases for scored candidates missing them."""
        candidates = store.fetchall(
            """
            SELECT candidate_id, normalized_form, surface_forms, candidate_type, examples,
                   description, suggested_aliases
            FROM governance.evolution_candidates
            WHERE %s::uuid = ANY(seen_source_doc_ids)
              AND composite_score > 0
              AND (description IS NULL OR description = '')
            LIMIT 5
            """,
            (source_doc_id,),
        )
        if not candidates:
            return 0

        try:
            from src.utils.llm_extract import LLMExtractor
            llm = LLMExtractor()
            if not llm.is_enabled():
                return 0
        except Exception:
            return 0

        import json as _json
        enriched = 0
        for cand in candidates:
            cid = cand["candidate_id"]
            normalized = cand.get("normalized_form", "")
            surface_forms = cand.get("surface_forms") or []
            ctype = cand.get("candidate_type", "concept")

            examples = cand.get("examples") or []
            if isinstance(examples, str):
                try:
                    examples = _json.loads(examples)
                except Exception:
                    examples = []
            texts = []
            for ex in examples[:3]:
                seg_id = ex.get("segment_id")
                if seg_id:
                    row = store.fetchone(
                        "SELECT raw_text FROM segments WHERE segment_id::text = %s", (str(seg_id),)
                    )
                    if row and row.get("raw_text"):
                        texts.append(row["raw_text"][:400])
            if not texts:
                continue

            layer_hint = {"concept": "configurable object", "mechanism": "protocol mechanism",
                          "method": "configuration/troubleshooting procedure",
                          "condition": "applicability condition or constraint",
                          "scenario": "deployment pattern"}.get(ctype, "concept")
            context = "\n---\n".join(texts)
            term = surface_forms[0] if surface_forms else normalized

            prompt = (
                f"Term: {term}\nKnowledge layer: {layer_hint}\n\n"
                f"Source text:\n{context}\n\n"
                f"Tasks:\n"
                f"1. Write a bilingual description (English first, then Chinese). Max 200 chars.\n"
                f"2. Suggest 3-5 aliases (English technical terms + Chinese translations).\n\n"
                f"Return JSON: {{\"description\": \"...\", \"aliases\": [\"...\", ...]}}"
            )
            try:
                raw = llm._call_llm(
                    "Generate description and aliases for a telecom ontology node. Return JSON only.",
                    prompt, 256
                )
                if not raw:
                    continue
                raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL).strip()
                raw = re.sub(r"```json\s*", "", raw)
                raw = re.sub(r"```\s*$", "", raw)
                result = _json.loads(raw)
                desc = result.get("description", "")[:400]
                aliases = result.get("aliases", [])
                if not isinstance(aliases, list):
                    aliases = []
                aliases = [str(a).strip() for a in aliases if a][:8]

                store.execute(
                    """UPDATE governance.evolution_candidates
                       SET description = %s, suggested_aliases = %s::jsonb
                       WHERE candidate_id = %s""",
                    (desc, _json.dumps(aliases, ensure_ascii=False), cid),
                )
                enriched += 1
                log.debug("Enriched candidate %s: desc=%d aliases=%d", str(cid)[:12], len(desc), len(aliases))
            except Exception as exc:
                log.debug("Failed to enrich candidate %s: %s", str(cid)[:12], exc)

        return enriched