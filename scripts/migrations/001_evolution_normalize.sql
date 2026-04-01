-- Add columns for candidate normalization and source tracking
-- NOTE: After migration 003, this table lives in governance schema.
-- Run against governance.evolution_candidates if re-applying.
ALTER TABLE evolution_candidates
  ADD COLUMN IF NOT EXISTS normalized_form TEXT,
  ADD COLUMN IF NOT EXISTS seen_source_doc_ids UUID[] DEFAULT '{}';

-- Unique index on normalized_form for upsert deduplication
CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_candidates_normalized
  ON evolution_candidates(normalized_form)
  WHERE normalized_form IS NOT NULL;