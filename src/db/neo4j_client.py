"""
Neo4j client — thin wrapper around the official neo4j driver.

Usage:
    from src.db.neo4j_client import get_session, run_query

    with get_session() as session:
        result = session.run("MATCH (n:OntologyNode) RETURN n LIMIT 5")
        for record in result:
            print(record["n"])

Or use run_query() for one-shot queries:
    rows = run_query("MATCH (n:OntologyNode {node_id: $id}) RETURN n", id="IP.BGP")
"""

import logging
from contextlib import contextmanager
from typing import Any, Generator

from neo4j import GraphDatabase, Driver, Session
from neo4j.exceptions import ServiceUnavailable

from src.config.settings import settings

logger = logging.getLogger(__name__)

_driver: Driver | None = None


def _get_driver() -> Driver:
    global _driver
    if _driver is None:
        logger.info("Connecting to Neo4j → %s", settings.NEO4J_URI)
        _driver = GraphDatabase.driver(
            settings.NEO4J_URI,
            auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
            # Keep a small connection pool; tune if needed
            max_connection_pool_size=20,
            connection_timeout=10,
        )
    return _driver


@contextmanager
def get_session() -> Generator[Session, None, None]:
    """Yield a Neo4j session against the configured database."""
    session = _get_driver().session(database=settings.NEO4J_DATABASE)
    try:
        yield session
    finally:
        session.close()


def run_query(cypher: str, **params: Any) -> list[dict[str, Any]]:
    """Execute a Cypher query and return results as a list of dicts."""
    with get_session() as session:
        result = session.run(cypher, **params)
        return [dict(record) for record in result]


def run_write(cypher: str, **params: Any) -> list[dict[str, Any]]:
    """Execute a write transaction and return results."""
    def _tx(tx):
        result = tx.run(cypher, **params)
        return [dict(record) for record in result]

    with get_session() as session:
        return session.execute_write(_tx)


def close_driver() -> None:
    """Gracefully close the driver (call on app shutdown)."""
    global _driver
    if _driver:
        _driver.close()
        _driver = None
        logger.info("Neo4j driver closed.")


def ping() -> bool:
    """Health-check: returns True if Neo4j is reachable."""
    try:
        _get_driver().verify_connectivity()
        return True
    except ServiceUnavailable as exc:
        logger.error("Neo4j ping failed: %s", exc)
        return False