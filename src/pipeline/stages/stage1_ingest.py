"""Stage 1: Ingest - rules C1-C5."""

from __future__ import annotations

import logging
import uuid

from semcore.core.context import PipelineContext
from semcore.core.types import Document
from semcore.pipeline.base import Stage
from semcore.providers.base import ObjectStore, RelationalStore

from src.crawler.extractor import ContentExtractor
from src.crawler.normalizer import DocumentNormalizer

log = logging.getLogger(__name__)
_extractor = ContentExtractor()
_normalizer = DocumentNormalizer()


class IngestStage(Stage):
    """semcore Stage wrapper - inputs via ctx.meta['crawl_task_id']."""

    name = "ingest"

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        crawl_task_id = ctx.meta.get("crawl_task_id")
        if crawl_task_id is None:
            ctx.record_error("IngestStage: crawl_task_id missing from ctx.meta")
            return ctx
        objects: ObjectStore | None = getattr(app, "objects", None)
        store: RelationalStore = app.store
        doc_dict = self._run(crawl_task_id, objects, store)
        if doc_dict:
            ctx.doc = Document(
                source_doc_id=doc_dict["source_doc_id"],
                doc_type=doc_dict.get("doc_type", "unknown"),
                attributes=doc_dict,
            )
        return ctx

    def _run(
        self, crawl_task_id: int, objects: ObjectStore | None, store: RelationalStore
    ) -> dict | None:
        """
        Rule C3: content_hash dedup.
        Rule C4: text extraction with quality gate.
        Rule C5: doc_type detection.
        Returns document dict or None (skipped/low quality).
        """
        task = store.fetchone(
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

        raw_html = self._load_raw(task, objects)
        if not raw_html:
            return None

        from src.utils.hashing import content_hash
        c_hash = content_hash(raw_html)
        existing = store.fetchone(
            "SELECT source_doc_id FROM documents WHERE content_hash = %s", (c_hash,)
        )
        if existing:
            log.info("Task %d: content_hash duplicate, skipping", crawl_task_id)
            store.execute(
                "UPDATE crawl_tasks SET status='deduped' WHERE id=%s", (crawl_task_id,)
            )
            return None

        extracted = _extractor.extract(raw_html, task["url"])
        # Plain-text docs (RFC .txt) need newlines preserved for structural splitting
        url = task.get("url", "")
        is_plaintext = url.endswith(".txt") or extracted.get("content_type", "").startswith("text/plain")
        clean_text = _normalizer.normalize(extracted["text"], preserve_newlines=is_plaintext)
        _, norm_hash = _normalizer.compute_hashes(raw_html, clean_text)
        cleaned_uri = self._store_cleaned_text(objects, norm_hash, clean_text)
        raw_uri = task.get("raw_storage_uri") or ""

        if extracted["is_low_quality"]:
            log.info("Task %d: low quality page, skipping", crawl_task_id)
            self._upsert_document(
                store, task, extracted, c_hash, norm_hash,
                raw_storage_uri=raw_uri,
                cleaned_storage_uri=cleaned_uri,
                status="low_quality",
            )
            return None

        doc_type = _extractor.detect_doc_type(
            task["url"], extracted["title"], extracted["text"]
        )

        dup_group = store.fetchone(
            "SELECT dedup_group_id FROM documents WHERE normalized_hash = %s", (norm_hash,)
        )
        dedup_group_id = dup_group["dedup_group_id"] if dup_group else str(uuid.uuid4())

        doc = self._upsert_document(
            store, task, extracted, c_hash, norm_hash,
            raw_storage_uri=raw_uri,
            cleaned_storage_uri=cleaned_uri,
            doc_type=doc_type,
            dedup_group_id=dedup_group_id,
            status="raw",
        )

        store.execute(
            """INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version)
               VALUES ('segmentation', %s, 'pending', '0.1.0')""",
            (doc["source_doc_id"],),
        )
        log.info(
            "Ingested task=%s doc=%s type=%s raw_uri=%s cleaned_uri=%s",
            crawl_task_id,
            doc["source_doc_id"],
            doc_type,
            raw_uri or "n/a",
            cleaned_uri or "n/a",
        )
        return doc

    def _upsert_document(
        self,
        store: RelationalStore,
        task: dict,
        extracted: dict,
        c_hash: str,
        norm_hash: str,
        *,
        raw_storage_uri: str,
        cleaned_storage_uri: str | None,
        doc_type: str = "unknown",
        dedup_group_id: str | None = None,
        status: str = "raw",
    ) -> dict:
        source_doc_id = str(uuid.uuid4())
        with store.transaction() as cur:
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
                    raw_storage_uri or None,
                    cleaned_storage_uri or None,
                ),
            )
        return {"source_doc_id": source_doc_id, "doc_type": doc_type, "status": status}

    def _load_raw(self, task: dict, objects: ObjectStore | None) -> str | None:
        """Load raw HTML from object storage or local cache."""
        uri = task.get("raw_storage_uri", "")
        if uri.startswith("minio://") and objects is not None:
            try:
                raw = objects.get(uri).decode("utf-8", errors="replace")
                log.info("Loaded raw html: task=%s uri=%s bytes=%s", task["id"], uri, len(raw))
                return raw
            except Exception as exc:
                log.error("Failed to load raw html: task=%s uri=%s err=%s", task["id"], uri, exc)
                return None
        if uri.startswith("local://") or uri.startswith("raw://"):
            return "<html>Stub HTML content for testing</html>"
        if not uri:
            log.warning("Task %d missing raw_storage_uri", task["id"])
        return None

    def _store_cleaned_text(
        self,
        objects: ObjectStore | None,
        norm_hash: str,
        clean_text: str,
    ) -> str | None:
        if objects is None:
            return None
        if not clean_text.strip():
            return None
        # Content-addressed key: same normalized text → same key (dedup),
        # different text → different key (no overwrite)
        key = f"cleaned/{norm_hash}.txt"
        try:
            uri = objects.put(
                key,
                clean_text.encode("utf-8", errors="replace"),
                content_type="text/plain",
            )
            log.info("Stored cleaned text: hash=%s uri=%s bytes=%s", norm_hash[:12], uri, len(clean_text))
            return uri
        except Exception as exc:
            log.error("Failed to store cleaned text: hash=%s err=%s", norm_hash[:12], exc)
            return None