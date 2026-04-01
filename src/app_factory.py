"""build_app() — composition root: wires all providers into a SemanticApp."""

from __future__ import annotations

from semcore.app import AppConfig, SemanticApp
from semcore.operators.base import LoggingMiddleware, TimingMiddleware


def build_app() -> SemanticApp:
    """Construct the telecom semantic KB application.

    All provider, governance, and pipeline objects are instantiated here and
    injected into SemanticApp via AppConfig.  Nothing outside this function
    should import from src.db or src.utils directly — use app.store / app.graph
    / app.llm / app.embedding instead.
    """
    from src.config.settings import settings

    # ── Providers ─────────────────────────────────────────────────────────────
    from src.providers.postgres_store          import PostgresRelationalStore
    from src.providers.neo4j_store             import Neo4jGraphStore
    from src.providers.anthropic_llm           import ClaudeLLMProvider
    from src.providers.bge_m3_embedding        import BGEM3EmbeddingProvider
    from src.providers.minio_store             import MinioObjectStore
    from src.providers.crawler_postgres_store  import CrawlerPostgresRelationalStore

    # ── Ontology ──────────────────────────────────────────────────────────────
    from src.ontology.registry    import OntologyRegistry
    from src.ontology.yaml_provider import YAMLOntologyProvider

    # ── Governance ────────────────────────────────────────────────────────────
    from src.governance.confidence_scorer import TelecomConfidenceScorer
    from src.governance.conflict_detector import TelecomConflictDetector
    from src.governance.evolution_gate    import TelecomEvolutionGate

    # ── Pipeline ──────────────────────────────────────────────────────────────
    from src.pipeline.pipeline_factory import build_pipeline

    # ── Operators ─────────────────────────────────────────────────────────────
    from src.operators import ALL_OPERATORS

    # ── Assembly ──────────────────────────────────────────────────────────────
    registry = OntologyRegistry.from_default()

    config = AppConfig(
        llm       = ClaudeLLMProvider(settings),
        embedding = BGEM3EmbeddingProvider(),
        graph     = Neo4jGraphStore(),
        store         = PostgresRelationalStore(),
        crawler_store = CrawlerPostgresRelationalStore(),
        objects       = MinioObjectStore(settings),
        ontology  = YAMLOntologyProvider(registry),
        confidence_scorer = TelecomConfidenceScorer(),
        conflict_detector = TelecomConflictDetector(),
        evolution_gate    = TelecomEvolutionGate(),
        operators   = ALL_OPERATORS,
        middlewares = [TimingMiddleware(), LoggingMiddleware()],
    )
    config.pipeline = build_pipeline()
    return SemanticApp(config)


# Module-level singleton — imported by app.py and router.py
_app: SemanticApp | None = None


def get_app() -> SemanticApp:
    """Return the module-level SemanticApp singleton (lazy init)."""
    global _app
    if _app is None:
        _app = build_app()
    return _app