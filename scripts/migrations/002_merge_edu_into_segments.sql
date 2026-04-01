-- =============================================================
-- Migration 002: Merge t_edu_detail into segments
--
-- Adds title/title_vec/content_vec/content_source columns to
-- segments, migrates data from t_edu_detail, updates t_rst_relation
-- FKs, then drops t_edu_detail.
-- =============================================================

-- Step 1: Add new columns to segments
ALTER TABLE segments ADD COLUMN IF NOT EXISTS title         VARCHAR(255);
ALTER TABLE segments ADD COLUMN IF NOT EXISTS title_vec     vector(1024);
ALTER TABLE segments ADD COLUMN IF NOT EXISTS content_vec   vector(1024);
ALTER TABLE segments ADD COLUMN IF NOT EXISTS content_source VARCHAR(128);

-- Step 2: Migrate data from t_edu_detail into segments
UPDATE segments s
SET title          = e.title,
    title_vec      = e.title_vec,
    content_vec    = e.content_vec,
    content_source = e.content_source
FROM t_edu_detail e
WHERE e.edu_id = s.segment_id::text;

-- Step 3: Update t_rst_relation FKs to reference segments
-- Drop old FKs referencing t_edu_detail
ALTER TABLE t_rst_relation DROP CONSTRAINT IF EXISTS t_rst_relation_src_edu_id_fkey;
ALTER TABLE t_rst_relation DROP CONSTRAINT IF EXISTS t_rst_relation_dst_edu_id_fkey;

-- Change column type from VARCHAR(64) to UUID to match segments.segment_id
ALTER TABLE t_rst_relation
    ALTER COLUMN src_edu_id TYPE UUID USING src_edu_id::uuid,
    ALTER COLUMN dst_edu_id TYPE UUID USING dst_edu_id::uuid;

-- Add new FKs referencing segments
ALTER TABLE t_rst_relation
    ADD CONSTRAINT t_rst_relation_src_edu_id_fkey FOREIGN KEY (src_edu_id) REFERENCES segments(segment_id),
    ADD CONSTRAINT t_rst_relation_dst_edu_id_fkey FOREIGN KEY (dst_edu_id) REFERENCES segments(segment_id);

-- Step 4: Drop t_edu_detail
DROP TABLE IF EXISTS t_edu_detail;