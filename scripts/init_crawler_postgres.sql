-- =============================================================
-- Telecom Semantic KB — Crawler PostgreSQL Schema
-- Separate database for crawl scheduling and pipeline job tracking.
-- Run: psql -h <host> -U postgres -d telecom_crawler -f init_crawler_postgres.sql
-- =============================================================

-- =============================================================
-- 1. source_registry
-- =============================================================
CREATE TABLE IF NOT EXISTS source_registry (
    id              SERIAL PRIMARY KEY,
    site_key        VARCHAR(64)   NOT NULL UNIQUE,
    site_name       VARCHAR(255)  NOT NULL,
    home_url        VARCHAR(1024) NOT NULL,
    source_rank     CHAR(1)       NOT NULL CHECK (source_rank IN ('S','A','B','C')),
    crawl_enabled   BOOLEAN       NOT NULL DEFAULT true,
    robots_policy   JSONB,
    rate_limit_rps  NUMERIC(5,2)  DEFAULT 1.0,
    seed_urls       JSONB,
    scope_rules     JSONB,
    extra_headers   JSONB,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT NOW()
);

-- =============================================================
-- 2. crawl_tasks
-- =============================================================
CREATE TABLE IF NOT EXISTS crawl_tasks (
    id              BIGSERIAL PRIMARY KEY,
    site_key        VARCHAR(64)  NOT NULL REFERENCES source_registry(site_key),
    url             TEXT         NOT NULL,
    canonical_url   TEXT,
    task_type       VARCHAR(32)  NOT NULL DEFAULT 'full',
    priority        SMALLINT     NOT NULL DEFAULT 5,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
    scheduled_at    TIMESTAMPTZ,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    retry_count     SMALLINT     NOT NULL DEFAULT 0,
    http_status     SMALLINT,
    error_msg       TEXT,
    parent_task_id  BIGINT       REFERENCES crawl_tasks(id),
    raw_storage_uri TEXT,
    content_hash    CHAR(64),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_crawl_tasks_url    ON crawl_tasks(url);
CREATE        INDEX IF NOT EXISTS idx_crawl_tasks_status ON crawl_tasks(status);
CREATE        INDEX IF NOT EXISTS idx_crawl_tasks_site   ON crawl_tasks(site_key);

-- =============================================================
-- 3. extraction_jobs
-- =============================================================
CREATE TABLE IF NOT EXISTS extraction_jobs (
    id               BIGSERIAL PRIMARY KEY,
    job_id           UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    job_type         VARCHAR(64)  NOT NULL,
    source_doc_id    UUID,        -- logical reference to documents.source_doc_id (cross-DB)
    status           VARCHAR(32)  NOT NULL DEFAULT 'pending',
    pipeline_version VARCHAR(32),
    config_snapshot  JSONB,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    error_msg        TEXT,
    stats            JSONB,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);