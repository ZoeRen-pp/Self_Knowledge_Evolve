# Embedding Enhancements — 5 Optimizations

Date: 2026-04-06
Status: Approved
Prerequisite: BAAI/bge-m3 model available locally, EMBEDDING_ENABLED=true

## 1. Stage 3 Alignment — Semantic Fuzzy Matching

**File**: `src/pipeline/stages/stage3_align.py` → `_find_terms`

Current: exact alias match only → misses "link-state advertisement" when ontology only has "LSA".

Enhancement: after exact matching, for segments with 0 canonical tags, compute embedding of segment text vs all ontology node names. Cosine > 0.80 → add as low-confidence tag (confidence=0.70, tagger='embedding').

Constraint: only triggers when exact match yields 0 canonical tags (avoids double-tagging).

## 2. Stage 5 Dedup — Semantic Fact Dedup

**File**: `src/pipeline/stages/stage5_dedup.py`

Current: SimHash text dedup only.

Enhancement: for fact pairs with same subject+object (or subject+object swapped), compute embedding of their source segment texts. Cosine > 0.90 → merge (keep higher confidence).

## 3. Conflict Detection — Embedding-Assisted

**File**: `src/api/semantic/conflict_detect.py`

Current: exact subject+object match to find potential conflicts.

Enhancement: find fact pairs where subject embedding ≈ subject AND object embedding ≈ object (cosine > 0.85) but predicates differ → flag as potential semantic conflict.

## 4. O5 Node Similarity — Third Signal

**File**: `src/stats/ontology_quality.py` → `_detect_similar_nodes`

Current: neighbor Jaccard + tag co-occurrence (2 signals).

Enhancement: add embedding cosine of node canonical names as third signal.
Combined score = 0.35 * neighbor_jaccard + 0.35 * tag_cooccurrence + 0.30 * embedding_cosine.

## 5. Review Synonym Check — Embedding Pre-filter

**File**: `src/api/system/review.py` → `check_synonyms`

Current: always calls LLM.

Enhancement: compute embedding cosine first. If > 0.90 → return synonym=true without LLM. If < 0.60 → return synonym=false without LLM. Only 0.60-0.90 range calls LLM. Saves ~70% of LLM calls.

## Changes Summary

| File | Change |
|------|--------|
| `src/pipeline/stages/stage3_align.py` | Add `_embedding_match` fallback in `_find_terms` |
| `src/pipeline/stages/stage5_dedup.py` | Add embedding similarity check for fact pairs |
| `src/api/semantic/conflict_detect.py` | Add embedding-based conflict candidate discovery |
| `src/stats/ontology_quality.py` | Add embedding cosine to O5 scoring |
| `src/api/system/review.py` | Embedding pre-filter before LLM synonym check |

All enhancements degrade gracefully: if EMBEDDING_ENABLED=false, original behavior is preserved.
