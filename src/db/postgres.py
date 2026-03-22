"""
PostgreSQL client — connection pool via psycopg2.

Usage:
    from src.db.postgres import get_conn, execute, fetchall

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")

Or use the thin helpers for simple queries.
"""

import logging
from contextlib import contextmanager
from typing import Any, Generator

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2.extras import RealDictCursor

from src.config.settings import settings

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        logger.info(
            "Initialising PostgreSQL pool → %s:%s/%s",
            settings.POSTGRES_HOST,
            settings.POSTGRES_PORT,
            settings.POSTGRES_DB,
        )
        _pool = pg_pool.ThreadedConnectionPool(
            minconn=settings.POSTGRES_POOL_MIN,
            maxconn=settings.POSTGRES_POOL_MAX,
            dsn=settings.postgres_dsn,
        )
    return _pool


@contextmanager
def get_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a connection from the pool; auto-commit or rollback on exit."""
    conn = _get_pool().getconn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)


def execute(sql: str, params: tuple = ()) -> None:
    """Fire-and-forget execute (INSERT / UPDATE / DELETE)."""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)


def fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Return rows as list of dicts."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            return [dict(row) for row in cur.fetchall()]


def fetchone(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    """Return a single row as dict, or None."""
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None


def close_pool() -> None:
    """Gracefully close all connections (call on app shutdown)."""
    global _pool
    if _pool:
        _pool.closeall()
        _pool = None
        logger.info("PostgreSQL pool closed.")


def ping() -> bool:
    """Health-check: returns True if DB is reachable."""
    try:
        result = fetchone("SELECT 1 AS ok")
        return result is not None
    except Exception as exc:
        logger.error("PostgreSQL ping failed: %s", exc)
        return False
