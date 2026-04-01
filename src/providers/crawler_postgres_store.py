"""CrawlerPostgresRelationalStore — RelationalStore for the crawler database."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator

from semcore.providers.base import RelationalStore
import src.db.crawler_postgres as cpg


class CrawlerPostgresRelationalStore(RelationalStore):
    def fetchone(self, sql: str, params: tuple | dict | None = None) -> dict[str, Any] | None:
        return cpg.fetchone(sql, params or ())

    def fetchall(self, sql: str, params: tuple | dict | None = None) -> list[dict[str, Any]]:
        return cpg.fetchall(sql, params or ())

    def execute(self, sql: str, params: tuple | dict | None = None) -> None:
        cpg.execute(sql, params or ())

    @contextmanager
    def transaction(self) -> Generator[Any, None, None]:
        with cpg.get_conn() as conn:
            with conn.cursor() as cur:
                yield cur