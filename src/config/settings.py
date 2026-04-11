from pydantic import computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ── PostgreSQL (knowledge DB) ────────────────────────────
    POSTGRES_HOST: str
    POSTGRES_PORT: int
    POSTGRES_DB: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_POOL_MIN: int
    POSTGRES_POOL_MAX: int
    POSTGRES_ADMIN_DB: str = "postgres"
    POSTGRES_AUTO_CREATE: bool = True

    # ── Crawler PostgreSQL (separate DB) ──────────────────
    CRAWLER_POSTGRES_HOST: str = ""
    CRAWLER_POSTGRES_PORT: int = 0
    CRAWLER_POSTGRES_DB: str = "telecom_crawler"
    CRAWLER_POSTGRES_USER: str = ""
    CRAWLER_POSTGRES_PASSWORD: str = ""
    CRAWLER_POSTGRES_POOL_MIN: int = 1
    CRAWLER_POSTGRES_POOL_MAX: int = 5
    CRAWLER_POSTGRES_AUTO_CREATE: bool = True

    @computed_field
    @property
    def postgres_dsn(self) -> str:
        host = self.POSTGRES_HOST
        if host == "localhost":
            host = "127.0.0.1"
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{host}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @computed_field
    @property
    def crawler_postgres_dsn(self) -> str:
        host = self.CRAWLER_POSTGRES_HOST or self.POSTGRES_HOST
        if host == "localhost":
            host = "127.0.0.1"
        port = self.CRAWLER_POSTGRES_PORT or self.POSTGRES_PORT
        user = self.CRAWLER_POSTGRES_USER or self.POSTGRES_USER
        password = self.CRAWLER_POSTGRES_PASSWORD or self.POSTGRES_PASSWORD
        return (
            f"postgresql://{user}:{password}"
            f"@{host}:{port}/{self.CRAWLER_POSTGRES_DB}"
        )

    # ── Neo4j ─────────────────────────────────────────────────
    NEO4J_URI: str = "bolt://localhost:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = "changeme"
    NEO4J_DATABASE: str = "neo4j"

    # ── MinIO / S3 ────────────────────────────────────────────
    MINIO_ENDPOINT: str = ""
    MINIO_ACCESS_KEY: str = "minioadmin"
    MINIO_SECRET_KEY: str = "minioadmin"
    MINIO_BUCKET_RAW: str = "telecom-kb-raw"
    MINIO_BUCKET_CLEANED: str = "telecom-kb-cleaned"
    MINIO_SECURE: bool = False

    # ── LLM (relation extraction) ─────────────────────────────
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    LLM_MODEL: str = "gemini-2.5-flash"
    LLM_MAX_TOKENS: int = 1024
    LLM_ENABLED: bool = False   # set True to enable LLM extraction

    # ── Embedding (BAAI/bge-m3, dim=1024) ────────────────────
    EMBEDDING_MODEL: str = "BAAI/bge-m3"
    EMBEDDING_DEVICE: str = "cpu"    # or "cuda"
    EMBEDDING_BATCH_SIZE: int = 32
    EMBEDDING_DIM: int = 1024
    EMBEDDING_ENABLED: bool = False  # set True after model is available

    # ── Ollama (preferred embedding backend) ─────────────────
    OLLAMA_URL: str = "http://localhost:11434"
    OLLAMA_EMBED_MODEL: str = "bge-m3"

    # ── Ontology Maintenance ─────────────────────────────────
    ONTOLOGY_MAINTENANCE_INTERVAL_HOURS: int = 24
    ONTOLOGY_MAINTENANCE_ENABLED: bool = True

    # ── Pipeline ──────────────────────────────────────────────
    ONTOLOGY_VERSION: str = "v0.2.0"
    LOG_LEVEL: str = "INFO"
    LOG_DIR: str = "logs"
    LOG_FILE_PREFIX: str = "app"
    LOG_FILE_MAX_MB: int = 5
    LOG_FILE_ENABLED: bool = True
    STARTUP_HEALTH_REQUIRED: bool = True

    WORKER_CRAWL_LIMIT: int = 10
    WORKER_PIPELINE_LIMIT: int = 10
    WORKER_PIPELINE_WORKERS: int = 4
    WORKER_SLEEP_SECS: int = 30


# Module-level singleton — import this everywhere
settings = Settings()
