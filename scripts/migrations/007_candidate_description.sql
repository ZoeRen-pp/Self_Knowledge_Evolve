-- Add description and suggested_aliases to evolution_candidates
-- Generated descriptions help reviewers assess candidates before approval

ALTER TABLE governance.evolution_candidates
  ADD COLUMN IF NOT EXISTS description TEXT DEFAULT '',
  ADD COLUMN IF NOT EXISTS suggested_aliases JSONB DEFAULT '[]'::jsonb;
