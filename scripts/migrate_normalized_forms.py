"""One-time migration: recompute normalized_form with new word-boundary-preserving normalization.

Usage:
    python scripts/migrate_normalized_forms.py [--dry-run]

What it does:
1. Reads all evolution_candidates
2. Recomputes normalized_form using the updated normalize_term (space-separated tokens)
3. Detects collisions (multiple old entries → same new normalized_form) and merges them
4. Updates the database

Run with --dry-run first to preview changes without writing.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "semcore"))
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import settings
from src.utils.normalize import normalize_term


def main():
    import psycopg2
    import psycopg2.extras

    dry_run = "--dry-run" in sys.argv

    conn = psycopg2.connect(dsn=settings.postgres_dsn)
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """SELECT candidate_id, normalized_form, surface_forms, source_count,
                  examples, seen_source_doc_ids, review_status
           FROM governance.evolution_candidates
           ORDER BY source_count DESC"""
    )
    rows = cur.fetchall()
    print(f"Total candidates: {len(rows)}")

    # Group by new normalized form to detect collisions
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        old_form = row["normalized_form"]
        # Reconstruct original term from surface_forms for re-normalization
        surface = (row["surface_forms"] or [None])[0] or old_form
        new_form = normalize_term(surface)
        groups[new_form].append(dict(row) | {"new_normalized": new_form, "old_normalized": old_form})

    unchanged = 0
    updated = 0
    merged = 0

    for new_form, members in groups.items():
        if len(members) == 1:
            m = members[0]
            if m["old_normalized"] == new_form:
                unchanged += 1
                continue
            # Simple rename — but check if target already exists (handled as merge in another group)
            if not dry_run:
                # Delete first then re-insert to avoid unique constraint
                # Actually: just delete this row and let the group with matching new_form absorb it
                # Check if target form exists
                cur.execute(
                    "SELECT candidate_id FROM governance.evolution_candidates WHERE normalized_form = %s AND candidate_id != %s",
                    (new_form, m["candidate_id"]),
                )
                if cur.fetchone():
                    # Target exists — delete this duplicate, the target group already merged it
                    cur.execute(
                        "DELETE FROM governance.evolution_candidates WHERE candidate_id = %s",
                        (m["candidate_id"],),
                    )
                    print(f"  ABSORBED: {m['old_normalized']} → {new_form} (target exists)")
                    merged += 1
                    continue
                cur.execute(
                    "UPDATE governance.evolution_candidates SET normalized_form = %s WHERE candidate_id = %s",
                    (new_form, m["candidate_id"]),
                )
            print(f"  RENAME: {m['old_normalized']} → {new_form}")
            updated += 1
        else:
            # Collision: multiple old entries map to same new form → merge
            # Keep the one with highest source_count as primary
            members.sort(key=lambda x: -(x.get("source_count") or 0))
            primary = members[0]
            others = members[1:]

            # Merge surface_forms, source_count, examples
            all_forms = list(primary.get("surface_forms") or [])
            total_count = int(primary.get("source_count") or 0)
            all_examples = list(primary.get("examples") or [])

            for other in others:
                for sf in (other.get("surface_forms") or []):
                    if sf not in all_forms:
                        all_forms.append(sf)
                total_count += int(other.get("source_count") or 0)
                other_ex = other.get("examples") or []
                if isinstance(other_ex, str):
                    other_ex = json.loads(other_ex)
                all_examples.extend(other_ex)

            print(f"  MERGE: {[m['old_normalized'] for m in members]} → {new_form} "
                  f"(primary={primary['candidate_id']}, forms={all_forms}, count={total_count})")

            if not dry_run:
                # Delete others first to avoid unique constraint violation
                for other in others:
                    cur.execute(
                        "DELETE FROM governance.evolution_candidates WHERE candidate_id = %s",
                        (other["candidate_id"],),
                    )
                cur.execute(
                    """UPDATE governance.evolution_candidates
                       SET normalized_form = %s, surface_forms = %s,
                           source_count = %s, examples = %s::jsonb
                       WHERE candidate_id = %s""",
                    (new_form, all_forms, total_count,
                     json.dumps(all_examples), primary["candidate_id"]),
                )
            merged += len(others)
            updated += 1

    if not dry_run:
        conn.commit()
    conn.close()

    print(f"\nDone {'(DRY RUN)' if dry_run else ''}:")
    print(f"  Unchanged: {unchanged}")
    print(f"  Updated:   {updated}")
    print(f"  Merged:    {merged} duplicates removed")


if __name__ == "__main__":
    main()