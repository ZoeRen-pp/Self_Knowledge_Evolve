from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── PostgreSQL ────────────────────────────────────────────
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_DB: str = "telecom_kb"
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "changeme"
    POSTGRES_POOL_MIN: int = 2
    POSTGRES_POOL_MAX: int = 10

    @computed_field
    @property
    def postgres_dsn(self) -> str:
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    # ── Neo4j ─────────────────────────────────────────────────
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "changeme"
    NEO4J_DATABASE: str = "neo4j"

    # ── MinIO / S3 ────────────────────────────────────────────
    MINIO_ENDPOINT: str = "localhost:9000"
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET_RAW: str = "telecom-kb-raw"
    MINIO_BUCKET_CLEANED: str = "telecom-kb-cleaned"
    MINIO_SECURE: bool = False

    # ── Pipeline ──────────────────────────────────────────────
    ONTOLOGY_VERSION: str = "v0.1.0"
    LOG_LEVEL: str = "INFO"


# Module-level singleton — import this everywhere
settings = Settings()
