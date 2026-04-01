-- =============================================================
-- Migration 003: Move governance tables to governance schema
-- =============================================================

CREATE SCHEMA IF NOT EXISTS governance;

ALTER TABLE IF EXISTS public.evolution_candidates SET SCHEMA governance;
ALTER TABLE IF EXISTS public.conflict_records SET SCHEMA governance;
ALTER TABLE IF EXISTS public.review_records SET SCHEMA governance;
ALTER TABLE IF EXISTS public.ontology_versions SET SCHEMA governance;