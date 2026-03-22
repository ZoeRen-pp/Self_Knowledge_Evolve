"""Stage 1: Ingest — rules C1-C5."""

from __future__ import annotations

import logging
import uuid

from src.crawler.extractor import ContentExtractor
from src.crawler.normalizer import DocumentNormalizer
from src.db.postgres import fetchone, execute, get_conn
from src.utils.confidence import score_segment

log = logging.getLogger(__name__)
_extractor = ContentExtractor()
_normalizer = DocumentNormalizer()


class IngestStage:
    def process(self, crawl_task_id: int) -> dict | None:
        """
        Rule C3: content_hash dedup.
        Rule C4: text extraction with quality gate.
        Rule C5: doc_type detection.
        Returns document dict or None (skipped/low quality).
        """
        task = fetchone(
            """
            SELECT ct.*, sr.source_rank, sr.site_key as sk
            FROM crawl_tasks ct
            JOIN source_registry sr ON ct.site_key = sr.site_key
            WHERE ct.id = %s
            """,
            (crawl_task_id,),
        )
        if not task:
            log.warning("Task %d not found", crawl_task_id)
            return None

        # Load raw HTML (stub: in real system fetch from object storage)
        raw_html = self._load_raw(task)
        if not raw_html:
            return None

        # Rule C3: hash dedup
        from src.utils.hashing import content_hash
        c_hash = content_hash(raw_html)
        existing = fetchone(
            "SELECT source_doc_id FROM documents WHERE content_hash = %s", (c_hash,)
        )
        if existing:
            log.info("Task %d: content_hash duplicate, skipping", crawl_task_id)
            execute(
                "UPDATE crawl_tasks SET status='deduped' WHERE id=%s", (crawl_task_id,)
            )
            return None

        # Rule C4: extract text
        extracted = _extractor.extract(raw_html, task["url"])
        clean_text = _normalizer.normalize(extracted["text"])
        _, norm_hash = _normalizer.compute_hashes(raw_html, clean_text)

        # Low quality gate
        if extracted["is_low_quality"]:
            log.info("Task %d: low quality page, skipping", crawl_task_id)
            self._upsert_document(task, extracted, c_hash, norm_hash, raw_html, clean_text, status="low_quality")
            return None

        # Rule C5: doc_type
        doc_type = _extractor.detect_doc_type(
            task["url"], extracted["title"], extracted["text"]
        )

        # Dedup group via normalized_hash
        dup_group = fetchone(
            "SELECT dedup_group_id FROM documents WHERE normalized_hash = %s", (norm_hash,)
        )
        dedup_group_id = dup_group["dedup_group_id"] if dup_group else str(uuid.uuid4())

        doc = self._upsert_document(
            task, extracted, c_hash, norm_hash, raw_html, clean_text,
            doc_type=doc_type, dedup_group_id=dedup_group_id, status="raw"
        )

        # Create extraction job for next stage
        execute(
            """INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version)
               VALUES ('segmentation', %s, 'pending', '0.1.0')""",
            (doc["source_doc_id"],),
        )
        log.info(
            "Task %d ingested → doc %s (type=%s)", crawl_task_id, doc["source_doc_id"], doc_type
        )
        return doc

    # ── Private ───────────────────────────────────────────────────

    def _upsert_document(
        self, task: dict, extracted: dict, c_hash: str, norm_hash: str,
        raw_html: str, clean_text: str, doc_type: str = "unknown",
        dedup_group_id: str | None = None, status: str = "raw",
    ) -> dict:
        source_doc_id = str(uuid.uuid4())
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO documents (
                        source_doc_id, crawl_task_id, site_key, source_url, canonical_url,
                        title, doc_type, language, source_rank, crawl_time,
                        content_hash, normalized_hash, status, dedup_group_id,
                        raw_storage_uri, cleaned_storage_uri
                    ) VALUES (
                        %s,%s,%s,%s,%s, %s,%s,%s,%s,NOW(),
                        %s,%s,%s,%s, %s,%s
                    )
                    ON CONFLICT (source_doc_id) DO NOTHING
                    RETURNING source_doc_id
                    """,
                    (
                        source_doc_id, task["id"], task["site_key"],
                        task["url"], task.get("canonical_url") or task["url"],
                        extracted["title"], doc_type, extracted["language"],
                        task["source_rank"], c_hash, norm_hash, status, dedup_group_id,
                        f"raw://{task['id']}", f"cleaned://{task['id']}",
                    ),
                )
        return {"source_doc_id": source_doc_id, "doc_type": doc_type, "status": status}

    def _load_raw(self, task: dict) -> str | None:
        """Stub: load raw HTML from object storage or local cache."""
        uri = task.get("raw_storage_uri", "")
        if uri.startswith("local://") or uri.startswith("raw://"):
            return "<html>Stub HTML content for testing</html>"
        return None
