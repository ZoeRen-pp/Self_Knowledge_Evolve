"""Pipeline orchestration — runs all 6 stages in sequence for a document.

NOTE: This is a legacy runner. Prefer using SemanticApp.ingest() which
properly runs stages through the semcore Pipeline with PipelineContext.
This runner is kept for batch operations that need direct task-level control.
"""

from __future__ import annotations

import logging
import time

from src.app_factory import get_app
from src.utils.logging import get_logger

log = get_logger(__name__)


class PipelineRunner:
    def __init__(self) -> None:
        self._app = get_app()

    def run_document(self, source_doc_id: str) -> dict:
        """Run the full semcore pipeline for one document."""
        from semcore.core.context import PipelineContext
        summary: dict = {"source_doc_id": source_doc_id, "stages_completed": [], "stats": {}}
        t0 = time.monotonic()

        ctx = PipelineContext(source_doc_id=source_doc_id)

        try:
            ctx = self._app.ingest_context(ctx)
            summary["stages_completed"] = self._app.pipeline_stages()
            if ctx.has_errors():
                summary["errors"] = ctx.errors
        except Exception as exc:
            log.error("Pipeline failed for doc %s: %s", source_doc_id, exc, exc_info=True)
            summary["status"] = "failed"
            summary["error"] = str(exc)
            return summary

        summary["elapsed_s"] = round(time.monotonic() - t0, 2)
        summary["status"] = "done"
        log.info(
            "Pipeline completed for doc %s in %.2fs",
            source_doc_id, summary["elapsed_s"],
        )
        return summary

    def run_batch(self, limit: int = 10) -> list[dict]:
        """Fetch documents in 'raw' status and run pipeline for each."""
        store = self._app.store
        rows = store.fetchall(
            "SELECT source_doc_id FROM documents WHERE status = 'raw' "
            "ORDER BY created_at ASC LIMIT %s",
            (limit,),
        )
        results = []
        for row in rows:
            result = self.run_document(str(row["source_doc_id"]))
            results.append(result)
        return results

    def run_pending(self, limit: int = 50) -> None:
        """Convenience: run batch and log summary."""
        log.info("Starting pipeline batch, limit=%d", limit)
        results = self.run_batch(limit)
        done    = sum(1 for r in results if r.get("status") == "done")
        skipped = sum(1 for r in results if r.get("status") == "skipped")
        failed  = sum(1 for r in results if r.get("status") == "failed")
        log.info("Batch complete: %d done, %d skipped, %d failed", done, skipped, failed)