-- Migration 006: Unify concept + relation candidates into one table

-- Add new columns to evolution_candidates
ALTER TABLE governance.evolution_candidates
  ADD COLUMN IF NOT EXISTS candidate_type VARCHAR(32) NOT NULL DEFAULT 'concept',
  ADD COLUMN IF NOT EXISTS examples JSONB DEFAULT '[]';

-- Migrate data from relation_candidates (if any exist)
INSERT INTO governance.evolution_candidates
    (surface_forms, normalized_form, candidate_type, examples, source_count,
     source_diversity_score, review_status, first_seen_at, last_seen_at)
SELECT
    ARRAY[predicate_name],
    normalized_name,
    'relation',
    examples,
    source_count,
    source_diversity,
    review_status,
    first_seen_at,
    last_seen_at
FROM governance.relation_candidates
ON CONFLICT (normalized_form) DO NOTHING;

-- Drop the separate table
DROP TABLE IF EXISTS governance.relation_candidates;
