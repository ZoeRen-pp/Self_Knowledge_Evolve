-- =============================================================
-- Telecom Semantic KB — PostgreSQL Schema Init
-- Run: psql -h <host> -U postgres -d telecom_kb -f init_postgres.sql
-- =============================================================

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";      -- pgvector for embeddings

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
-- 3. documents
-- =============================================================
CREATE TABLE IF NOT EXISTS documents (
    id                  BIGSERIAL PRIMARY KEY,
    source_doc_id       UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    crawl_task_id       BIGINT       REFERENCES crawl_tasks(id),
    site_key            VARCHAR(64)  NOT NULL REFERENCES source_registry(site_key),
    source_url          TEXT         NOT NULL,
    canonical_url       TEXT,
    title               TEXT,
    doc_type            VARCHAR(32),
    language            CHAR(5)      NOT NULL DEFAULT 'en',
    source_rank         CHAR(1)      NOT NULL,
    publish_time        TIMESTAMPTZ,
    crawl_time          TIMESTAMPTZ  NOT NULL,
    version_hint        VARCHAR(128),
    content_hash        CHAR(64),
    normalized_hash     CHAR(64),
    raw_storage_uri     TEXT,
    cleaned_storage_uri TEXT,
    struct_storage_uri  TEXT,
    page_structure      JSONB,
    status              VARCHAR(32)  NOT NULL DEFAULT 'raw',
    dedup_group_id      UUID,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_documents_canonical_url   ON documents(canonical_url);
CREATE INDEX IF NOT EXISTS idx_documents_normalized_hash ON documents(normalized_hash);
CREATE INDEX IF NOT EXISTS idx_documents_site_key        ON documents(site_key);
CREATE INDEX IF NOT EXISTS idx_documents_status          ON documents(status);

-- =============================================================
-- 4. segments
-- =============================================================
CREATE TABLE IF NOT EXISTS segments (
    id                  BIGSERIAL PRIMARY KEY,
    segment_id          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    source_doc_id       UUID         NOT NULL REFERENCES documents(source_doc_id),
    section_path        TEXT[],
    section_title       TEXT,
    segment_index       INTEGER      NOT NULL,
    segment_type        VARCHAR(32)  NOT NULL,
    raw_text            TEXT         NOT NULL,
    normalized_text     TEXT,
    token_count         INTEGER,
    confidence          NUMERIC(4,3) DEFAULT 1.0,
    dedup_signature     CHAR(64),
    simhash_value       BIGINT,
    embedding_ref       TEXT,
    embedding           vector(1536),    -- pgvector column; adjust dim to your model
    lifecycle_state     VARCHAR(32)  NOT NULL DEFAULT 'active',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_segments_source_doc_id ON segments(source_doc_id);
CREATE INDEX IF NOT EXISTS idx_segments_type          ON segments(segment_type);
CREATE INDEX IF NOT EXISTS idx_segments_simhash       ON segments(simhash_value);
-- Vector ANN index (create after bulk-loading data):
-- CREATE INDEX ON segments USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- =============================================================
-- 5. segment_tags
-- =============================================================
CREATE TABLE IF NOT EXISTS segment_tags (
    id               BIGSERIAL PRIMARY KEY,
    segment_id       UUID         NOT NULL REFERENCES segments(segment_id),
    tag_type         VARCHAR(32)  NOT NULL,
    tag_value        VARCHAR(256) NOT NULL,
    ontology_node_id VARCHAR(128),
    confidence       NUMERIC(4,3) DEFAULT 1.0,
    tagger           VARCHAR(64),
    ontology_version VARCHAR(32),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_segment_tags_segment_id ON segment_tags(segment_id);
CREATE INDEX IF NOT EXISTS idx_segment_tags_tag_value  ON segment_tags(tag_value);
CREATE INDEX IF NOT EXISTS idx_segment_tags_tag_type   ON segment_tags(tag_type);

-- =============================================================
-- 6. facts
-- =============================================================
CREATE TABLE IF NOT EXISTS facts (
    id               BIGSERIAL PRIMARY KEY,
    fact_id          UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    subject          VARCHAR(256) NOT NULL,
    predicate        VARCHAR(128) NOT NULL,
    object           VARCHAR(256) NOT NULL,
    qualifier        JSONB,
    domain           VARCHAR(128),
    confidence       NUMERIC(4,3) NOT NULL DEFAULT 0.5,
    lifecycle_state  VARCHAR(32)  NOT NULL DEFAULT 'active',
    merge_cluster_id UUID,
    ontology_version VARCHAR(32),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_facts_subject   ON facts(subject);
CREATE INDEX IF NOT EXISTS idx_facts_predicate ON facts(predicate);
CREATE INDEX IF NOT EXISTS idx_facts_object    ON facts(object);
CREATE INDEX IF NOT EXISTS idx_facts_cluster   ON facts(merge_cluster_id);

-- =============================================================
-- 7. evidence
-- =============================================================
CREATE TABLE IF NOT EXISTS evidence (
    id                  BIGSERIAL PRIMARY KEY,
    evidence_id         UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id             UUID         NOT NULL REFERENCES facts(fact_id),
    source_doc_id       UUID         NOT NULL REFERENCES documents(source_doc_id),
    segment_id          UUID         REFERENCES segments(segment_id),
    exact_span          TEXT,
    span_offset_start   INTEGER,
    span_offset_end     INTEGER,
    source_rank         CHAR(1)      NOT NULL,
    extraction_method   VARCHAR(64),
    evidence_score      NUMERIC(4,3) DEFAULT 0.5,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_evidence_fact_id    ON evidence(fact_id);
CREATE INDEX IF NOT EXISTS idx_evidence_segment_id ON evidence(segment_id);

-- =============================================================
-- 8. conflict_records
-- =============================================================
CREATE TABLE IF NOT EXISTS conflict_records (
    id             BIGSERIAL PRIMARY KEY,
    conflict_id    UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id_a      UUID         NOT NULL REFERENCES facts(fact_id),
    fact_id_b      UUID         NOT NULL REFERENCES facts(fact_id),
    conflict_type  VARCHAR(64)  NOT NULL,
    description    TEXT,
    resolution     VARCHAR(32)  DEFAULT 'open',
    resolved_by    VARCHAR(128),
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================
-- 9. ontology_versions
-- =============================================================
CREATE TABLE IF NOT EXISTS ontology_versions (
    id             SERIAL PRIMARY KEY,
    version_tag    VARCHAR(32)  NOT NULL UNIQUE,
    description    TEXT,
    snapshot_uri   TEXT,
    diff_from_prev JSONB,
    published_by   VARCHAR(128),
    published_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    status         VARCHAR(32)  NOT NULL DEFAULT 'active',
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================
-- 10. evolution_candidates
-- =============================================================
CREATE TABLE IF NOT EXISTS evolution_candidates (
    id                       BIGSERIAL PRIMARY KEY,
    candidate_id             UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_forms            TEXT[]       NOT NULL,
    normalized_form          VARCHAR(256),
    candidate_parent_id      VARCHAR(128),
    source_count             INTEGER      NOT NULL DEFAULT 0,
    source_diversity_score   NUMERIC(4,3) DEFAULT 0.0,
    temporal_stability_score NUMERIC(4,3) DEFAULT 0.0,
    structural_fit_score     NUMERIC(4,3) DEFAULT 0.0,
    retrieval_gain_score     NUMERIC(4,3) DEFAULT 0.0,
    synonym_risk_score       NUMERIC(4,3) DEFAULT 0.0,
    composite_score          NUMERIC(4,3) DEFAULT 0.0,
    review_status            VARCHAR(32)  NOT NULL DEFAULT 'discovered',
    reviewer                 VARCHAR(128),
    review_note              TEXT,
    first_seen_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    last_seen_at             TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    accepted_at              TIMESTAMPTZ,
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================
-- 11. review_records
-- =============================================================
CREATE TABLE IF NOT EXISTS review_records (
    id           BIGSERIAL PRIMARY KEY,
    review_id    UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    object_type  VARCHAR(64)  NOT NULL,
    object_id    UUID         NOT NULL,
    action       VARCHAR(64)  NOT NULL,
    reviewer     VARCHAR(128) NOT NULL,
    note         TEXT,
    before_state JSONB,
    after_state  JSONB,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================
-- 12. lexicon_aliases
-- =============================================================
CREATE TABLE IF NOT EXISTS lexicon_aliases (
    id               BIGSERIAL PRIMARY KEY,
    alias_id         UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    surface_form     TEXT         NOT NULL,
    canonical_node_id VARCHAR(128) NOT NULL,
    alias_type       VARCHAR(32)  NOT NULL,
    vendor           VARCHAR(64),
    language         CHAR(5)      DEFAULT 'en',
    confidence       NUMERIC(4,3) DEFAULT 1.0,
    source_doc_id    UUID         REFERENCES documents(source_doc_id),
    ontology_version VARCHAR(32),
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    UNIQUE (surface_form, canonical_node_id)
);

CREATE INDEX IF NOT EXISTS idx_lexicon_aliases_surface ON lexicon_aliases(surface_form);

-- =============================================================
-- 13. extraction_jobs
-- =============================================================
CREATE TABLE IF NOT EXISTS extraction_jobs (
    id               BIGSERIAL PRIMARY KEY,
    job_id           UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    job_type         VARCHAR(64)  NOT NULL,
    source_doc_id    UUID         REFERENCES documents(source_doc_id),
    status           VARCHAR(32)  NOT NULL DEFAULT 'pending',
    pipeline_version VARCHAR(32),
    config_snapshot  JSONB,
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    error_msg        TEXT,
    stats            JSONB,
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================
-- Seed: initial ontology version record
-- =============================================================
INSERT INTO ontology_versions (version_tag, description, status)
VALUES ('v0.1.0', 'Initial IP/datacommunication subdomain ontology', 'active')
ON CONFLICT (version_tag) DO NOTHING;
