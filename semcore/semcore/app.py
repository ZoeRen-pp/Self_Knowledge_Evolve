"""SemanticApp — the composition root that wires all layers together."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from semcore.core.context import PipelineContext
from semcore.governance.base import ConfidenceScorer, ConflictDetector, EvolutionGate
from semcore.ontology.base import OntologyProvider
from semcore.operators.base import (
    LoggingMiddleware,
    OperatorMiddleware,
    OperatorRegistry,
    OperatorResult,
    SemanticOperator,
    TimingMiddleware,
)
from semcore.pipeline.base import Pipeline, Stage
from semcore.providers.base import (
    EmbeddingProvider,
    GraphStore,
    LLMProvider,
    ObjectStore,
    RelationalStore,
)


# ---------------------------------------------------------------------------
# AppConfig — pure data, no behaviour
# ---------------------------------------------------------------------------

@dataclass
class AppConfig:
    """All wiring in one place.  Construct this, pass it to SemanticApp.

    Providers
    ---------
    Every provider field is required.  If a capability is genuinely absent
    (e.g. no object storage), supply a no-op implementation of the ABC.

    Governance
    ----------
    All three governance components are required.  Use the ``NullXxx`` stubs
    in ``semcore.governance.stubs`` during early development if you haven't
    implemented them yet.

    Middleware
    ----------
    ``middlewares`` defaults to [TimingMiddleware(), LoggingMiddleware()] so
    every operator is timed and logged without any explicit configuration.
    Pass an empty list to disable built-in middleware.

    Pipeline stages and operators
    -----------------------------
    These are injected as lists of instances.  The pipeline stages are
    appended in order; operators are registered by name.
    """

    # ── Providers ─────────────────────────────────────────────────────────────
    llm:        LLMProvider
    embedding:  EmbeddingProvider
    graph:      GraphStore
    store:      RelationalStore
    objects:    ObjectStore
    crawler_store: RelationalStore | None = None  # separate DB for crawler tables

    # ── Knowledge ─────────────────────────────────────────────────────────────
    ontology:   OntologyProvider

    # ── Governance ────────────────────────────────────────────────────────────
    confidence_scorer:  ConfidenceScorer
    conflict_detector:  ConflictDetector
    evolution_gate:     EvolutionGate

    # ── Pipeline ──────────────────────────────────────────────────────────────
    # Supply Stage instances in execution order.  If you need branching, build
    # the Pipeline manually and assign it to ``pipeline`` instead of using
    # this list.
    pipeline_stages: list[Stage] = field(default_factory=list)
    pipeline:        Pipeline | None = field(default=None, init=False)

    # ── Operators ─────────────────────────────────────────────────────────────
    operators:   list[SemanticOperator] = field(default_factory=list)

    # ── Middleware ────────────────────────────────────────────────────────────
    middlewares: list[OperatorMiddleware] = field(
        default_factory=lambda: [TimingMiddleware(), LoggingMiddleware()]
    )

    # ── Misc ──────────────────────────────────────────────────────────────────
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# SemanticApp — the composition root
# ---------------------------------------------------------------------------

class SemanticApp:
    """Wires providers, pipeline, operators, and governance into a runtime.

    Construction::

        app = SemanticApp(config)

    Usage::

        ctx = app.ingest("some-doc-id")
        result = app.query("lookup", term="BGP", lang="en")

    Advanced pipeline (with conditional routing)::

        from semcore.pipeline.base import Pipeline
        config.pipeline = (
            Pipeline()
            .add_stage(IngestStage())
            .branch(
                condition=lambda ctx, _app: ctx.doc.doc_type == "rfc",
                if_true=RFCSegmentStage(),
                if_false=DefaultSegmentStage(),
            )
            .add_stage(AlignStage())
        )
        app = SemanticApp(config)
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config

        # ── Providers ─────────────────────────────────────────────────────────
        self.llm:       LLMProvider       = config.llm
        self.embedding: EmbeddingProvider = config.embedding
        self.graph:     GraphStore        = config.graph
        self.store:     RelationalStore   = config.store
        self.objects:   ObjectStore       = config.objects
        self.crawler_store: RelationalStore | None = config.crawler_store

        # ── Ontology ──────────────────────────────────────────────────────────
        self.ontology:  OntologyProvider  = config.ontology

        # ── Governance ────────────────────────────────────────────────────────
        self.confidence_scorer: ConfidenceScorer = config.confidence_scorer
        self.conflict_detector: ConflictDetector = config.conflict_detector
        self.evolution_gate:    EvolutionGate    = config.evolution_gate

        # ── Pipeline ──────────────────────────────────────────────────────────
        if config.pipeline is not None:
            # Custom pipeline (with branches / switches) supplied directly
            self._pipeline = config.pipeline
        else:
            # Build linear pipeline from stage list
            self._pipeline = Pipeline()
            for stage in config.pipeline_stages:
                self._pipeline.add_stage(stage)

        # ── Operators ─────────────────────────────────────────────────────────
        self._registry = OperatorRegistry()
        for mw in config.middlewares:
            self._registry.use(mw)
        for op in config.operators:
            self._registry.register(op)

    # ── Public API ────────────────────────────────────────────────────────────

    def ingest(self, source_doc_id: str) -> PipelineContext:
        """Run the full pipeline for *source_doc_id*."""
        return self._pipeline.run(source_doc_id, self)

    def ingest_context(self, ctx: PipelineContext) -> PipelineContext:
        """Run the full pipeline for a pre-built context."""
        return self._pipeline.run_context(ctx, self)

    def ingest_from(
        self,
        stage_name: str,
        ctx: PipelineContext,
    ) -> PipelineContext:
        """Resume the pipeline from *stage_name* using an existing context.

        Useful for re-running a single stage during debugging or recovery.
        """
        return self._pipeline.run_from(stage_name, ctx, self)

    def query(self, op_name: str, **kwargs: Any) -> OperatorResult:
        """Execute the named semantic operator with given keyword arguments."""
        return self._registry.execute(op_name, self, **kwargs)

    def list_operators(self) -> list[str]:
        """Return the names of all registered operators."""
        return self._registry.list_names()

    def pipeline_stages(self) -> list[str]:
        """Return the names of all top-level pipeline nodes."""
        return self._pipeline.stage_names()
