"""Pipeline orchestration — runs all 6 stages in sequence for a document."""

from __future__ import annotations

import logging
import time

from src.db.postgres import fetchall, execute
from src.pipeline.stages.stage1_ingest import IngestStage
from src.pipeline.stages.stage2_segment import SegmentStage
from src.pipeline.stages.stage3_align import AlignStage
from src.pipeline.stages.stage4_extract import ExtractStage
from src.pipeline.stages.stage5_dedup import DedupStage
from src.pipeline.stages.stage6_index import IndexStage
from src.utils.logging import get_logger

log = get_logger(__name__)


class PipelineRunner:
    def __init__(self) -> None:
        self._ingest  = IngestStage()
        self._segment = SegmentStage()
        self._align   = AlignStage()
        self._extract = ExtractStage()
        self._dedup   = DedupStage()
        self._index   = IndexStage()

    def run_document(self, crawl_task_id: int) -> dict:
        """Run all 6 stages for one crawl task. Returns summary dict."""
        summary: dict = {"crawl_task_id": crawl_task_id, "stages_completed": [], "stats": {}}
        t0 = time.monotonic()

        # Stage 1: Ingest
        try:
            doc = self._ingest.process(crawl_task_id)
            if not doc:
                summary["status"] = "skipped"
                return summary
            source_doc_id = doc["source_doc_id"]
            summary["source_doc_id"] = source_doc_id
            summary["stages_completed"].append("ingest")
        except Exception as exc:
            log.error("Stage1 failed for task %d: %s", crawl_task_id, exc, exc_info=True)
            summary["status"] = "failed"; summary["error"] = str(exc)
            return summary

        # Stages 2-6: operate on source_doc_id
        stage_fns = [
            ("segment",  lambda: self._segment.process(source_doc_id)),
            ("align",    lambda: self._align.process(source_doc_id)),
            ("extract",  lambda: self._extract.process(source_doc_id)),
            ("dedup_seg",lambda: self._dedup.process_document(source_doc_id)),
            ("dedup_fact",lambda: self._dedup.process_facts(source_doc_id)),
            ("index",    lambda: self._index.process(source_doc_id)),
        ]

        for stage_name, fn in stage_fns:
            try:
                result = fn()
                summary["stages_completed"].append(stage_name)
                if isinstance(result, dict):
                    summary["stats"].update(result)
                elif isinstance(result, list):
                    summary["stats"][f"{stage_name}_count"] = len(result)
            except Exception as exc:
                log.error("Stage %s failed for doc %s: %s", stage_name, source_doc_id, exc, exc_info=True)
                summary["stages_completed"].append(f"{stage_name}:FAILED")
                # Continue to next stage (partial pipeline)

        summary["elapsed_s"] = round(time.monotonic() - t0, 2)
        summary["status"] = "done"
        log.info(
            "Pipeline completed for task %d (doc %s) in %.2fs",
            crawl_task_id, source_doc_id, summary["elapsed_s"],
        )
        return summary

    def run_batch(self, limit: int = 10) -> list[dict]:
        """Fetch pending tasks and run pipeline for each."""
        tasks = fetchall(
            """
            SELECT ct.id FROM crawl_tasks ct
            WHERE ct.status = 'done'
              AND NOT EXISTS (
                SELECT 1 FROM documents d WHERE d.crawl_task_id = ct.id AND d.status != 'raw'
              )
            ORDER BY ct.priority DESC, ct.id ASC
            LIMIT %s
            """,
            (limit,),
        )
        results = []
        for task in tasks:
            result = self.run_document(task["id"])
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