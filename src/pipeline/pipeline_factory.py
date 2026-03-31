"""build_pipeline() — assembles the 6-stage pipeline with conditional routing."""

from __future__ import annotations

from semcore.pipeline.base import Pipeline

from src.pipeline.stages.stage1_ingest  import IngestStage
from src.pipeline.stages.stage2_segment import SegmentStage
from src.pipeline.stages.stage3_align    import AlignStage
from src.pipeline.stages.stage3b_evolve  import EvolveStage
from src.pipeline.stages.stage4_extract  import ExtractStage
from src.pipeline.stages.stage5_dedup   import DedupStage
from src.pipeline.stages.stage6_index   import IndexStage


def build_pipeline() -> Pipeline:
    """Return the default telecom KB pipeline.

    Routing logic
    -------------
    Stage 2 (segmentation) is selected by doc_type via a switch node:
    - "rfc"  → RFCSegmentStage  (placeholder: uses default until implemented)
    - "cli"  → CLISegmentStage  (placeholder: uses default until implemented)
    - other  → SegmentStage (current general-purpose implementation)

    All other stages are linear.
    """
    default_segment = SegmentStage()

    return (
        Pipeline()
        .add_stage(IngestStage())
        .switch(
            key=lambda ctx, _app: (ctx.doc.doc_type if ctx.doc else "unknown"),
            branches={
                # Future domain-specific segmenters go here:
                # "rfc":   RFCSegmentStage(),
                # "cli":   CLISegmentStage(),
            },
            default=default_segment,
        )
        .add_stage(AlignStage())
        .add_stage(EvolveStage())
        .add_stage(ExtractStage())
        .add_stage(DedupStage())
        .add_stage(IndexStage())
    )