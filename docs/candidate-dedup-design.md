# Candidate Dedup Design — Fragmentation Treatment

Date: 2026-04-06
Status: Approved

## Problem

Pipeline Stage 3 candidate discovery produces excessive fragmented candidates:
- Same concept split into variants: "router id" (23 sources), "ospf router id" (5), "bgp router-id" (2), "router ids" (3)
- Pure noise: "this", "rfc", "figure" from regex fallback
- 3000+ candidates with <200 worth reviewing

Root causes:
1. LLM only extracts, doesn't classify — no judgment on whether a term is new vs variant
2. Regex fallback catches any capitalized word when LLM is unavailable
3. Normalization strips word boundaries, making downstream dedup impossible

## Solution: Three Layers

### Layer 1: Normalization — Preserve Word Boundaries

**File**: `src/utils/normalize.py`

Rewrite `normalize_term` to produce space-separated lowercase tokens instead of concatenated blob:

```
Before: "OSPF Router-ID" → "ospfrouterid"
After:  "OSPF Router-ID" → "ospf router id"
```

Rules:
- Lowercase all tokens
- Strip parenthetical content: "xxx (YYY)" → "xxx"
- Strip leading articles: "the/a/an"
- Hyphens: preserve when both sides are single uppercase letters or known abbreviations (IS-IS, MPLS-TE); otherwise replace with space (router-id → router id)
- Simple plural stripping: trailing "s" on non-acronym tokens >3 chars (routers→router, networks→network; NOT "BGPs", "QoS")
- Collapse version markers: "v4" → "4" (BGPv4 → bgp 4)

Breaking change: requires one-time migration of existing `normalized_form` values in `governance.evolution_candidates`.

### Layer 2: Smart Candidate Discovery — LLM Classification + Embedding Dedup

**Files**: `src/utils/llm_extract.py`, `src/pipeline/stages/stage3_align.py`

#### 2a: LLM Classification (primary)

Change `extract_candidate_terms` to require classification per term:

```json
{
  "term": "OSPF router ID",
  "classification": "variant",
  "parent_concept": "router ID",
  "reason": "router ID scoped to OSPF context"
}
```

Three classifications:
- `new_concept` → enters candidate pool normally
- `variant` → discarded (contextual mention of existing concept, not a new concept)
- `noise` → discarded

LLM prompt updated with:
- Explicit instruction to classify, not just extract
- Negative examples (qualified variants, plurals, generic words)
- "precision over recall" principle

#### 2b: Embedding Dedup (safety net)

When a `new_concept` candidate passes LLM classification, compute its embedding (bge-m3 if available) and check cosine similarity against:
- Existing ontology node names/aliases → cosine > 0.85 → treat as variant, skip
- Existing candidate normalized_forms → cosine > 0.85 → merge surface_form into existing candidate

This catches what LLM misses: cross-document duplicates, synonyms, and edge cases.

Falls back gracefully: if embedding model not loaded (`EMBEDDING_ENABLED=false`), skip this check — LLM classification alone still works.

#### 2c: Remove Regex Fallback

Delete the regex fallback path (`[A-Z][A-Za-z0-9\-]{2,}|[A-Z]{2,10}`). When LLM is unavailable, this segment produces zero candidates. Quality over quantity — next pipeline run with LLM available will discover them.

### Layer 3: Minimal Safety Net — Stopword List

**File**: `ontology/patterns/candidate_stopwords.yaml`

A short list (~50 words) of obvious non-concepts as last insurance against LLM occasionally misclassifying common English words:

```yaml
stopwords:
  - this
  - that
  - these
  - figure
  - table
  - section
  - example
  - note
  - however
  - therefore
  - also
  # ... etc
```

Applied after LLM classification, before upsert. Only rejects exact token matches against single-token candidates. Does NOT do conceptual judgment.

## Changes Summary

| File | Change |
|------|--------|
| `src/utils/normalize.py` | Rewrite `normalize_term`, preserve word boundaries |
| `src/utils/llm_extract.py` | `extract_candidate_terms` prompt: add classification |
| `src/pipeline/stages/stage3_align.py` | Remove regex fallback, add embedding dedup check, add stopword filter |
| `ontology/patterns/candidate_stopwords.yaml` | New file, stopword list |
| `scripts/migrate_normalized_forms.py` | One-time migration script |

No changes to: database schema, API, dashboard, other pipeline stages.

## Architecture Position

```
Stage 3: _collect_candidates
  │
  ├── LLM extract_candidate_terms (with classification)
  │     ├── new_concept → continue
  │     ├── variant → discard
  │     └── noise → discard
  │
  ├── [NO regex fallback — LLM unavailable = 0 candidates]
  │
  ├── Stopword filter (last insurance)
  │
  ├── Embedding dedup (if EMBEDDING_ENABLED)
  │     ├── cosine > 0.85 vs ontology node → discard
  │     └── cosine > 0.85 vs existing candidate → merge
  │
  └── _upsert_candidates (survivors only)
```