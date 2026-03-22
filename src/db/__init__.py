from src.db.postgres import ping as pg_ping
from src.db.neo4j_client import ping as neo4j_ping


def health_check() -> dict:
    return {
        "postgres": pg_ping(),
        "neo4j": neo4j_ping(),
    }