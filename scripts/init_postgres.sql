-- =============================================================
-- Telecom Semantic KB — PostgreSQL Schema Init
-- Run: psql -h <host> -U postgres -d telecom_kb -f init_postgres.sql
-- =============================================================

-- Enable extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "vector";      -- pgvector for embeddings

-- Governance schema for evolution/conflict/review tables
CREATE SCHEMA IF NOT EXISTS governance;

-- NOTE: source_registry, crawl_tasks, and extraction_jobs have been moved
-- to the separate telecom_crawler database. See init_crawler_postgres.sql.

-- =============================================================
-- 1. documents
-- =============================================================
CREATE TABLE IF NOT EXISTS documents (
    id                  BIGSERIAL PRIMARY KEY,
    source_doc_id       UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    crawl_task_id       BIGINT,      -- logical reference to crawler DB crawl_tasks.id
    site_key            VARCHAR(64)  NOT NULL,
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
    embedding           vector(1024),    -- BAAI/bge-m3 produces 1024-dim vectors
    title               VARCHAR(255),                          -- generated summary / section heading (merged from t_edu_detail)
    title_vec           vector(1024),                          -- bge-m3 embedding of title
    content_vec         vector(1024),                          -- bge-m3 embedding of content text
    content_source      VARCHAR(128),                          -- site_key:canonical_url
    lifecycle_state     VARCHAR(32)  NOT NULL DEFAULT 'active',
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_segments_source_doc_id ON segments(source_doc_id);
CREATE INDEX IF NOT EXISTS idx_segments_type          ON segments(segment_type);
CREATE INDEX IF NOT EXISTS idx_segments_simhash       ON segments(simhash_value);
-- Vector ANN index (create after bulk-loading data, requires pgvector >= 0.5):
-- CREATE INDEX ON segments USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
-- For HNSW (pgvector >= 0.5, better recall):
-- CREATE INDEX ON segments USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- Vector ANN indexes for segments (create after bulk load):
-- CREATE INDEX ON segments USING hnsw (title_vec   vector_cosine_ops) WITH (m=16, ef_construction=64);
-- CREATE INDEX ON segments USING hnsw (content_vec vector_cosine_ops) WITH (m=16, ef_construction=64);

-- =============================================================
-- 4a. t_rst_relation  (RST discourse relations between EDUs)
-- Captures semantic-logical connections between EDU pairs,
-- serving as the discourse graph before ontology alignment.
-- =============================================================
CREATE TABLE IF NOT EXISTS t_rst_relation (
    nn_relation_id  VARCHAR(36)   NOT NULL PRIMARY KEY,   -- UUID
    relation_type   VARCHAR(255)  NOT NULL,                -- paragraph-level discourse type
    nuclearity      VARCHAR(2)    NOT NULL DEFAULT 'NN',   -- NS | SN | NN
    src_edu_id      UUID          NOT NULL REFERENCES segments(segment_id),
    dst_edu_id      UUID          NOT NULL REFERENCES segments(segment_id),
    meta_context    JSONB,         -- {"SYNTACTIC_ORDER": <int>, "src_type": "...", "dst_type": "..."}
    relation_source VARCHAR(255),  -- rule / llm / manual
    update_time     TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    reliability     BIGINT        NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_rst_src ON t_rst_relation(src_edu_id);
CREATE INDEX IF NOT EXISTS idx_rst_dst ON t_rst_relation(dst_edu_id);
CREATE INDEX IF NOT EXISTS idx_rst_type ON t_rst_relation(relation_type);

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
-- 8. governance.conflict_records
-- =============================================================
CREATE TABLE IF NOT EXISTS governance.conflict_records (
    id             BIGSERIAL PRIMARY KEY,
    conflict_id    UUID         NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    fact_id_a      UUID         NOT NULL REFERENCES public.facts(fact_id),
    fact_id_b      UUID         NOT NULL REFERENCES public.facts(fact_id),
    conflict_type  VARCHAR(64)  NOT NULL,
    description    TEXT,
    resolution     VARCHAR(32)  DEFAULT 'open',
    resolved_by    VARCHAR(128),
    resolved_at    TIMESTAMPTZ,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- =============================================================
-- 9. governance.ontology_versions
-- =============================================================
CREATE TABLE IF NOT EXISTS governance.ontology_versions (
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
-- 10. governance.evolution_candidates
-- =============================================================
CREATE TABLE IF NOT EXISTS governance.evolution_candidates (
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
    created_at               TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    candidate_type           VARCHAR(32)  NOT NULL DEFAULT 'concept',
                             -- concept | relation
    examples                 JSONB        DEFAULT '[]'
);

-- =============================================================
-- 11. governance.review_records
-- =============================================================
CREATE TABLE IF NOT EXISTS governance.review_records (
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

-- NOTE: extraction_jobs moved to telecom_crawler database.

-- =============================================================
-- Seed: initial ontology version record
-- =============================================================
INSERT INTO governance.ontology_versions (version_tag, description, status)
VALUES ('v0.1.0', 'Initial IP/datacommunication subdomain ontology', 'active')
ON CONFLICT (version_tag) DO NOTHING;

INSERT INTO governance.ontology_versions (version_tag, description, status)
VALUES ('v0.2.0', 'Five-layer semantic structure: Concept/Mechanism/Method/Condition/Scenario', 'active')
ON CONFLICT (version_tag) DO NOTHING;

-- NOTE: relation_candidates merged into evolution_candidates (candidate_type='relation')

-- =============================================================
-- System monitoring: stats snapshots
-- =============================================================
CREATE TABLE IF NOT EXISTS system_stats_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    snapshot    JSONB NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stats_created ON system_stats_snapshots(created_at);
