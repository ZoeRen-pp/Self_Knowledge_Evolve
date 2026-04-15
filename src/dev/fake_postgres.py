"""
In-memory PostgreSQL substitute backed by SQLite.
Replaces src.db.postgres so operators can run without a real PG instance.

Converts psycopg2-style %s placeholders → SQLite ? placeholders.
Seeded via src.dev.seed.seed_from_registry().
"""

from __future__ import annotations

import re
import sqlite3
import logging
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger(__name__)

# Module-level SQLite connection (in-memory, shared across threads is fine for dev)
_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(":memory:", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema()
        logger.info("fake_postgres: SQLite in-memory DB initialised")
    return _conn


def _init_schema() -> None:
    c = _get_conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS lexicon_aliases (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            surface_form    TEXT NOT NULL,
            canonical_node_id TEXT NOT NULL,
            alias_type      TEXT DEFAULT 'synonym',
            language        TEXT DEFAULT 'en',
            vendor          TEXT DEFAULT NULL,
            confidence      REAL DEFAULT 0.9
        );
        CREATE TABLE IF NOT EXISTS documents (
            source_doc_id   TEXT PRIMARY KEY,
            crawl_task_id   INTEGER,
            site_key        TEXT,
            source_url      TEXT,
            canonical_url   TEXT,
            title           TEXT,
            doc_type        TEXT DEFAULT 'unknown',
            language        TEXT DEFAULT 'en',
            source_rank     TEXT DEFAULT 'C',
            crawl_time      TEXT,
            content_hash    TEXT,
            normalized_hash TEXT,
            status          TEXT DEFAULT 'active',
            lifecycle_state TEXT DEFAULT 'active',
            dedup_group_id  TEXT,
            raw_storage_uri TEXT,
            cleaned_storage_uri TEXT
        );
        CREATE TABLE IF NOT EXISTS segments (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id      TEXT NOT NULL UNIQUE,
            source_doc_id   TEXT,
            section_path    TEXT,
            section_title   TEXT,
            segment_index   INTEGER DEFAULT 0,
            segment_type    TEXT,
            raw_text        TEXT,
            normalized_text TEXT,
            token_count     INTEGER DEFAULT 0,
            simhash_value   INTEGER,
            confidence      REAL DEFAULT 0.5,
            lifecycle_state TEXT DEFAULT 'active',
            title           TEXT,
            title_vec       TEXT,
            content_vec     TEXT,
            content_source  TEXT
        );
        CREATE TABLE IF NOT EXISTS segment_tags (
            tag_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            segment_id      TEXT,
            tag_type        TEXT DEFAULT 'canonical',
            tag_value       TEXT,
            ontology_node_id TEXT,
            confidence      REAL DEFAULT 0.8,
            tagger          TEXT DEFAULT 'rule',
            ontology_version TEXT DEFAULT 'v0.1.0'
        );
        CREATE TABLE IF NOT EXISTS facts (
            fact_id         TEXT PRIMARY KEY,
            subject         TEXT,
            predicate       TEXT,
            object          TEXT,
            qualifier       TEXT DEFAULT '{}',
            domain          TEXT,
            confidence      REAL DEFAULT 0.5,
            lifecycle_state TEXT DEFAULT 'active',
            merge_cluster_id TEXT,
            ontology_version TEXT
        );
        CREATE TABLE IF NOT EXISTS evidence (
            evidence_id     TEXT PRIMARY KEY,
            fact_id         TEXT,
            source_doc_id   TEXT,
            segment_id      TEXT,
            source_rank     TEXT DEFAULT 'C',
            extraction_method TEXT DEFAULT 'rule',
            evidence_score  REAL DEFAULT 0.5,
            exact_span      TEXT
        );
        CREATE TABLE IF NOT EXISTS conflict_records (
            conflict_id     TEXT PRIMARY KEY,
            fact_id_a       TEXT,
            fact_id_b       TEXT,
            conflict_type   TEXT
        );
        CREATE TABLE IF NOT EXISTS evolution_candidates (
            candidate_id    INTEGER PRIMARY KEY AUTOINCREMENT,
            surface_forms   TEXT,
            normalized_form TEXT UNIQUE,
            source_count    INTEGER DEFAULT 1,
            first_seen_at   TEXT,
            last_seen_at    TEXT,
            review_status   TEXT DEFAULT 'discovered',
            seen_source_doc_ids TEXT DEFAULT '{}',
            candidate_parent_id TEXT,
            source_diversity_score REAL DEFAULT 0,
            temporal_stability_score REAL DEFAULT 0,
            structural_fit_score REAL DEFAULT 0,
            synonym_risk_score REAL DEFAULT 0,
            composite_score REAL DEFAULT 0,
            candidate_type TEXT DEFAULT 'concept',
            examples TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS t_rst_relation (
            nn_relation_id  TEXT PRIMARY KEY,
            relation_type   TEXT,
            nuclearity      TEXT DEFAULT 'NN',
            src_edu_id      TEXT,
            dst_edu_id      TEXT,
            meta_context    TEXT,
            relation_source TEXT
        );
        CREATE TABLE IF NOT EXISTS system_stats_snapshots (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            snapshot        TEXT,
            created_at      TEXT DEFAULT (datetime('now'))
        );
    """)
    c.commit()


def _to_sqlite(sql: str) -> str:
    """Convert psycopg2 syntax to SQLite-compatible syntax."""
    sql = sql.replace("governance.", "")          # strip schema prefix (SQLite has no schemas)
    sql = re.sub(r"%s::\w+", "?", sql)           # strip PG type casts (%s::jsonb → ?)
    sql = re.sub(r"ARRAY\[%s\]", "%s", sql)      # ARRAY[%s] → %s (store as plain text)
    # ANY(%s) → IN (SELECT value FROM json_each(?))
    # Params are JSON-serialised lists; json_each unpacks them for IN-list matching.
    sql = re.sub(r"=\s*ANY\(%s\)", "IN (SELECT value FROM json_each(?))", sql)
    sql = re.sub(r"=\s*ANY\(\?\)", "IN (SELECT value FROM json_each(?))", sql)
    return sql.replace("%s", "?")


def _normalise_params(params) -> tuple:
    """Ensure params is a tuple (handles list or None).

    List/tuple values that are *elements* of the params sequence are
    JSON-serialised so SQLite can store or unpack them:
    - Array-field values (e.g. section_path=[]) → stored as JSON text.
    - ANY(%s) list params → also JSON text; _to_sqlite rewrites the clause
      to use json_each(?) so SQLite unpacks them for IN-list matching.
    """
    import json as _json
    if params is None:
        return ()
    if isinstance(params, (list, tuple)):
        result = []
        for p in params:
            if isinstance(p, (list, tuple)):
                result.append(_json.dumps(p))
            else:
                result.append(p)
        return tuple(result)
    return (params,)


def _deserialise_row(row: dict) -> dict:
    """Restore list/dict values that were JSON-serialised on write.

    _normalise_params serialises Python lists → JSON strings so SQLite can
    store them.  On read-back we reverse that for any string that looks like
    a JSON array ('[…]') or object ('{…}'), which covers columns such as
    section_path, surface_forms, seen_source_doc_ids, examples, etc.
    Non-JSON strings are returned unchanged.
    """
    import json as _json
    result = {}
    for k, v in row.items():
        if isinstance(v, str) and len(v) >= 2 and (
            (v[0] == "[" and v[-1] == "]") or (v[0] == "{" and v[-1] == "}")
        ):
            try:
                result[k] = _json.loads(v)
            except ValueError:
                result[k] = v
        else:
            result[k] = v
    return result


def fetchall(sql: str, params=()) -> list[dict[str, Any]]:
    sql_lite = _to_sqlite(sql)
    # Handle PostgreSQL ANY(%s) → SQLite IN (?) workaround: not needed for dev queries
    try:
        cur = _get_conn().execute(sql_lite, _normalise_params(params))
        return [_deserialise_row(dict(row)) for row in cur.fetchall()]
    except sqlite3.OperationalError as e:
        logger.debug("fake_postgres.fetchall skipped unsupported query: %s | %s", e, sql[:80])
        return []


def fetchone(sql: str, params=()) -> dict[str, Any] | None:
    rows = fetchall(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params=()) -> None:
    sql_lite = _to_sqlite(sql)
    try:
        _get_conn().execute(sql_lite, _normalise_params(params))
        _get_conn().commit()
    except sqlite3.OperationalError as e:
        logger.debug("fake_postgres.execute skipped unsupported query: %s | %s", e, sql[:80])


@contextmanager
def get_conn() -> Generator:
    """Yield a thin wrapper that exposes cursor() for stage code that uses get_conn()."""
    yield _FakeConn(_get_conn())


@contextmanager
def transaction() -> Generator:
    """Yield a cursor-like object for bulk inserts inside a transaction block."""
    yield _FakeCursor(_get_conn())


def ping() -> bool:
    try:
        _get_conn().execute("SELECT 1")
        return True
    except Exception:
        return False


def close_pool() -> None:
    global _conn
    if _conn:
        _conn.close()
        _conn = None


# ── Minimal connection/cursor wrappers for code that does with get_conn() as conn ──

class _FakeCursor:
    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._conn = sqlite_conn
        self._rows: list = []

    def execute(self, sql: str, params=()):
        sql_lite = _to_sqlite(sql)
        try:
            cur = self._conn.execute(sql_lite, _normalise_params(params))
            self._rows = [_deserialise_row(dict(r)) for r in cur.fetchall()]
            self._conn.commit()
        except sqlite3.OperationalError as e:
            logger.debug("_FakeCursor.execute skipped: %s | %s", e, sql[:80])
            self._rows = []

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeConn:
    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._conn = sqlite_conn

    def cursor(self, cursor_factory=None):
        return _FakeCursor(self._conn)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass