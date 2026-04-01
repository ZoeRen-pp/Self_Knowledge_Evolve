"""
In-memory SQLite substitute for the crawler PostgreSQL database.
Contains source_registry, crawl_tasks, and extraction_jobs tables.
"""

from __future__ import annotations

import re
import sqlite3
import logging
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger(__name__)

_conn: sqlite3.Connection | None = None


def _get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(":memory:", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _init_schema()
        logger.info("fake_crawler_postgres: SQLite in-memory DB initialised")
    return _conn


def _init_schema() -> None:
    c = _get_conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS source_registry (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            site_key        TEXT NOT NULL UNIQUE,
            site_name       TEXT,
            home_url        TEXT,
            source_rank     TEXT DEFAULT 'C',
            crawl_enabled   INTEGER DEFAULT 1,
            rate_limit_rps  REAL DEFAULT 1.0,
            seed_urls       TEXT,
            scope_rules     TEXT,
            extra_headers   TEXT,
            robots_policy   TEXT
        );
        CREATE TABLE IF NOT EXISTS crawl_tasks (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            site_key        TEXT,
            url             TEXT UNIQUE,
            canonical_url   TEXT,
            task_type       TEXT DEFAULT 'full',
            priority        INTEGER DEFAULT 5,
            status          TEXT DEFAULT 'pending',
            scheduled_at    TEXT,
            started_at      TEXT,
            finished_at     TEXT,
            retry_count     INTEGER DEFAULT 0,
            http_status     INTEGER,
            error_msg       TEXT,
            parent_task_id  INTEGER,
            raw_storage_uri TEXT,
            content_hash    TEXT
        );
        CREATE TABLE IF NOT EXISTS extraction_jobs (
            job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type        TEXT,
            source_doc_id   TEXT,
            status          TEXT DEFAULT 'pending',
            pipeline_version TEXT
        );
    """)
    c.commit()


def _to_sqlite(sql: str) -> str:
    sql = sql.replace("governance.", "")
    sql = re.sub(r"%s::\w+", "?", sql)
    sql = re.sub(r"ARRAY\[%s\]", "%s", sql)
    return sql.replace("%s", "?")


def _normalise_params(params) -> tuple:
    if params is None:
        return ()
    if isinstance(params, (list, tuple)):
        result = []
        for p in params:
            if isinstance(p, (list, tuple)):
                result.extend(p)
            else:
                result.append(p)
        return tuple(result)
    return (params,)


def fetchall(sql: str, params=()) -> list[dict[str, Any]]:
    sql_lite = _to_sqlite(sql)
    try:
        cur = _get_conn().execute(sql_lite, _normalise_params(params))
        return [dict(row) for row in cur.fetchall()]
    except sqlite3.OperationalError as e:
        logger.debug("fake_crawler_postgres.fetchall skipped: %s | %s", e, sql[:80])
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
        logger.debug("fake_crawler_postgres.execute skipped: %s | %s", e, sql[:80])


@contextmanager
def get_conn() -> Generator:
    yield _FakeConn(_get_conn())


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


class _FakeCursor:
    def __init__(self, sqlite_conn: sqlite3.Connection):
        self._conn = sqlite_conn
        self._rows: list = []

    def execute(self, sql: str, params=()):
        sql_lite = _to_sqlite(sql)
        try:
            cur = self._conn.execute(sql_lite, _normalise_params(params))
            self._rows = [dict(r) for r in cur.fetchall()]
            self._conn.commit()
        except sqlite3.OperationalError as e:
            logger.debug("fake_crawler_postgres._FakeCursor.execute skipped: %s | %s", e, sql[:80])
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