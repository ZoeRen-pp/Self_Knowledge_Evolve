-- Add columns for candidate normalization and source tracking
ALTER TABLE evolution_candidates
  ADD COLUMN IF NOT EXISTS normalized_form TEXT,
  ADD COLUMN IF NOT EXISTS seen_source_doc_ids UUID[] DEFAULT '{}';

-- Unique index on normalized_form for upsert deduplication
CREATE UNIQUE INDEX IF NOT EXISTS idx_evolution_candidates_normalized
  ON evolution_candidates(normalized_form)
  WHERE normalized_form IS NOT NULL;