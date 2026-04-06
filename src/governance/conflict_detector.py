"""TelecomConflictDetector — ConflictDetector extracted from stage5_dedup logic."""

from __future__ import annotations

from semcore.core.types import Fact
from semcore.governance.base import Conflict, ConflictDetector
from semcore.providers.base import RelationalStore

import logging

log = logging.getLogger(__name__)


class TelecomConflictDetector(ConflictDetector):
    def detect(self, fact: Fact, store: RelationalStore) -> list[Conflict]:
        """Find conflicts: exact match + embedding-based semantic match."""
        log.debug("conflict_detect: %s %s %s", fact.subject, fact.predicate, fact.object)

        # Exact match: same subject+predicate, different object
        rows = store.fetchall(
            """
            SELECT fact_id FROM facts
            WHERE subject = %s AND predicate = %s AND object != %s
              AND lifecycle_state = 'active'
            """,
            (fact.subject, fact.predicate, fact.object),
        )
        conflicts = [
            Conflict(
                fact_id_a=fact.fact_id,
                fact_id_b=row["fact_id"],
                conflict_type="contradictory_value",
                description=(
                    f"{fact.subject} {fact.predicate} has conflicting objects"
                ),
            )
            for row in rows
        ]

        # Embedding match: semantically similar subject+object but different predicate
        emb_conflicts = self._embedding_conflict_detect(fact, store)
        conflicts.extend(emb_conflicts)

        if conflicts:
            log.info("conflict_detect: %s %s → %d conflicts (%d exact, %d semantic)",
                     fact.subject, fact.predicate, len(conflicts),
                     len(conflicts) - len(emb_conflicts), len(emb_conflicts))
        return conflicts

    def _embedding_conflict_detect(self, fact: Fact, store: RelationalStore) -> list[Conflict]:
        """Find facts where subject≈subject AND object≈object but predicate differs."""
        from src.config.settings import settings
        if not getattr(settings, "EMBEDDING_ENABLED", False):
            return []

        try:
            from src.utils.embedding import get_embeddings
            import numpy as np

            # Get a sample of active facts with different predicates
            candidates = store.fetchall(
                """SELECT fact_id, subject, predicate, object FROM facts
                   WHERE predicate != %s AND lifecycle_state = 'active'
                   AND (subject = %s OR object = %s)
                   LIMIT 50""",
                (fact.predicate, fact.subject, fact.object),
            )
            if not candidates:
                return []

            # Encode subject+object pairs
            my_text = f"{fact.subject} {fact.object}".lower()
            texts = [my_text] + [f"{c['subject']} {c['object']}".lower() for c in candidates]
            vecs = get_embeddings(texts)
            if vecs is None:
                return []

            emb = np.array(vecs)
            my_vec = emb[0]
            similarities = np.dot(emb[1:], my_vec)

            THRESHOLD = 0.85
            conflicts = []
            for i, sim in enumerate(similarities):
                if float(sim) >= THRESHOLD:
                    c = candidates[i]
                    conflicts.append(Conflict(
                        fact_id_a=fact.fact_id,
                        fact_id_b=c["fact_id"],
                        conflict_type="semantic_contradiction",
                        description=(
                            f"Semantic conflict: ({fact.subject}, {fact.predicate}, {fact.object}) "
                            f"vs ({c['subject']}, {c['predicate']}, {c['object']}) "
                            f"cosine={float(sim):.3f}"
                        ),
                    ))
            return conflicts
        except Exception as exc:
            log.debug("Embedding conflict detection failed: %s", exc)
            return []