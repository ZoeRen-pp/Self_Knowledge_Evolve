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
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Generator

import psycopg2
from psycopg2 import pool as pg_pool
from psycopg2 import sql as pg_sql
from psycopg2.extras import RealDictCursor

from src.config.settings import settings

logger = logging.getLogger(__name__)

_pool: pg_pool.ThreadedConnectionPool | None = None
_db_checked = False


def _connect(db_name: str):
    host = settings.POSTGRES_HOST
    if host == "localhost":
        host = "127.0.0.1"
    return psycopg2.connect(
        dbname=db_name,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
        host=host,
        port=settings.POSTGRES_PORT,
    )


def _split_sql_script(sql: str) -> list[str]:
    lines = []
    for line in sql.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    cleaned = "\n".join(lines)
    return [stmt.strip() for stmt in cleaned.split(";") if stmt.strip()]


def _run_init_sql() -> None:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "init_postgres.sql"
    if not script_path.exists():
        logger.warning("PostgreSQL init script not found: %s", script_path)
        return

    sql = script_path.read_text(encoding="utf-8")
    statements = _split_sql_script(sql)
    if not statements:
        logger.warning("PostgreSQL init script is empty: %s", script_path)
        return

    with _connect(settings.POSTGRES_DB) as conn:
        with conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)
        conn.commit()

    logger.info("PostgreSQL schema initialised from %s", script_path.name)


def _schema_ready() -> bool:
    try:
        with _connect(settings.POSTGRES_DB) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT to_regclass('public.documents') IS NOT NULL")
                row = cur.fetchone()
                return bool(row and row[0])
    except Exception as exc:
        logger.error("PostgreSQL schema check failed: %s", exc)
        return False


def _ensure_database() -> None:
    global _db_checked
    if _db_checked or not settings.POSTGRES_AUTO_CREATE:
        return

    exists = True
    conn = None
    try:
        conn = _connect(settings.POSTGRES_ADMIN_DB)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (settings.POSTGRES_DB,),
            )
            exists = cur.fetchone() is not None
            if not exists:
                logger.info("PostgreSQL database missing; creating: %s", settings.POSTGRES_DB)
                cur.execute(
                    pg_sql.SQL("CREATE DATABASE {}").format(
                        pg_sql.Identifier(settings.POSTGRES_DB)
                    )
                )
    except Exception as exc:
        logger.error("PostgreSQL auto-create failed: %s", exc)
        _db_checked = False
        raise
    finally:
        if conn is not None:
            conn.close()

    if not exists:
        _run_init_sql()
    elif not _schema_ready():
        logger.info("PostgreSQL schema missing; initialising from SQL script.")
        _run_init_sql()
    _db_checked = True


def _get_pool() -> pg_pool.ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _ensure_database()
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
            # TCP keepalive: prevent NAT/firewall from killing idle connections
            keepalives=1,
            keepalives_idle=60,       # start probing after 60s idle
            keepalives_interval=15,   # probe every 15s
            keepalives_count=4,       # give up after 4 missed probes
        )
    return _pool


def _is_conn_alive(conn: psycopg2.extensions.connection) -> bool:
    """Check if a pooled connection is still usable."""
    if conn.closed:
        return False
    try:
        conn.cursor().execute("SELECT 1")
        conn.rollback()  # discard the trivial result without affecting txn state
        return True
    except Exception:
        return False


@contextmanager
def get_conn() -> Generator[psycopg2.extensions.connection, None, None]:
    """Yield a connection from the pool; auto-commit or rollback on exit.

    Dead connections (server-side timeout, network drop) are detected and
    replaced transparently so callers never see 'connection already closed'.
    """
    p = _get_pool()
    conn = p.getconn()

    if not _is_conn_alive(conn):
        logger.warning("Discarding dead pooled connection; replacing with fresh one")
        p.putconn(conn, close=True)
        conn = p.getconn()

    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass  # rollback may fail on a broken conn; swallow to raise original
        raise
    finally:
        # Return to pool only if still usable; otherwise discard
        if conn.closed:
            p.putconn(conn, close=True)
        else:
            p.putconn(conn)


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
        ok = result is not None
        if ok:
            logger.info("PostgreSQL ping ok.")
        return ok
    except Exception as exc:
        logger.error("PostgreSQL ping failed: %s", exc)
        return False
