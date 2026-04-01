"""Stage 1: Ingest (Clean) — rules C3-C5.

Assumes the document record already exists in the `documents` table and
raw content is stored in MinIO (raw_storage_uri). This stage:
  C3: content-hash dedup (skip if duplicate)
  C4: text extraction with quality gate
  C5: doc_type detection
  → writes cleaned text back to MinIO, updates the document record.

Document creation is the responsibility of the data source (crawler, upload
handler, file importer, etc.) and happens *before* the pipeline starts.
"""

from __future__ import annotations

import logging
import uuid

from semcore.core.context import PipelineContext
from semcore.core.types import Document
from semcore.pipeline.base import Stage
from semcore.providers.base import ObjectStore, RelationalStore

from src.pipeline.preprocessing.extractor import ContentExtractor
from src.pipeline.preprocessing.normalizer import DocumentNormalizer

log = logging.getLogger(__name__)
_extractor = ContentExtractor()
_normalizer = DocumentNormalizer()


class IngestStage(Stage):
    """Pipeline Stage 1: load raw document → clean → quality gate → update record."""

    name = "ingest"

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        source_doc_id = ctx.source_doc_id or ctx.meta.get("source_doc_id")
        if not source_doc_id:
            ctx.record_error("IngestStage: source_doc_id missing")
            return ctx

        objects: ObjectStore | None = getattr(app, "objects", None)
        store: RelationalStore = app.store
        doc_dict = self._run(source_doc_id, objects, store)
        if doc_dict:
            ctx.doc = Document(
                source_doc_id=doc_dict["source_doc_id"],
                doc_type=doc_dict.get("doc_type", "unknown"),
                attributes=doc_dict,
            )
        return ctx

    def _run(
        self, source_doc_id: str, objects: ObjectStore | None, store: RelationalStore,
    ) -> dict | None:
        # Load existing document record
        doc = store.fetchone(
            "SELECT * FROM documents WHERE source_doc_id = %s", (source_doc_id,)
        )
        if not doc:
            log.error("Document %s not found", source_doc_id)
            return None

        # Already cleaned? Skip.
        if doc.get("status") not in ("raw", None):
            log.info("Document %s already processed (status=%s), skipping", source_doc_id, doc["status"])
            return doc

        # Load raw content from MinIO
        raw_content = self._load_raw(doc, objects)
        if not raw_content:
            return None

        # C3: content-hash dedup
        from src.utils.hashing import content_hash
        c_hash = content_hash(raw_content)
        existing = store.fetchone(
            "SELECT source_doc_id FROM documents WHERE content_hash = %s AND source_doc_id != %s",
            (c_hash, source_doc_id),
        )
        if existing:
            log.info("Document %s: content_hash duplicate of %s, marking deduped",
                     source_doc_id, existing["source_doc_id"])
            store.execute(
                "UPDATE documents SET status='deduped', content_hash=%s WHERE source_doc_id=%s",
                (c_hash, source_doc_id),
            )
            return None

        # C4: text extraction + quality gate
        source_url = doc.get("source_url") or ""
        extracted = _extractor.extract(raw_content, source_url)

        is_plaintext = (
            source_url.endswith(".txt")
            or extracted.get("content_type", "").startswith("text/plain")
        )
        clean_text = _normalizer.normalize(extracted["text"], preserve_newlines=is_plaintext)
        _, norm_hash = _normalizer.compute_hashes(raw_content, clean_text)

        # Store cleaned text to MinIO
        cleaned_uri = self._store_cleaned_text(objects, norm_hash, clean_text)

        if extracted["is_low_quality"]:
            log.info("Document %s: low quality, marking", source_doc_id)
            store.execute(
                """UPDATE documents SET status='low_quality', content_hash=%s,
                   normalized_hash=%s, cleaned_storage_uri=%s,
                   title=COALESCE(title,%s), language=COALESCE(language,%s)
                   WHERE source_doc_id=%s""",
                (c_hash, norm_hash, cleaned_uri,
                 extracted["title"], extracted["language"], source_doc_id),
            )
            return None

        # C5: doc_type detection
        doc_type = _extractor.detect_doc_type(
            source_url, extracted["title"], extracted["text"]
        )

        # Normalized-hash dedup grouping
        dup_group = store.fetchone(
            "SELECT dedup_group_id FROM documents WHERE normalized_hash = %s AND source_doc_id != %s",
            (norm_hash, source_doc_id),
        )
        dedup_group_id = dup_group["dedup_group_id"] if dup_group else str(uuid.uuid4())

        # Update the document record with cleaning results
        store.execute(
            """UPDATE documents SET
                content_hash=%s, normalized_hash=%s,
                cleaned_storage_uri=%s,
                title=COALESCE(%s, title),
                doc_type=%s, language=%s,
                dedup_group_id=%s, status='cleaned'
               WHERE source_doc_id=%s""",
            (
                c_hash, norm_hash, cleaned_uri,
                extracted["title"], doc_type, extracted["language"],
                dedup_group_id, source_doc_id,
            ),
        )

        log.info(
            "Cleaned doc=%s type=%s cleaned_uri=%s",
            source_doc_id, doc_type, cleaned_uri or "n/a",
        )
        return {
            "source_doc_id": source_doc_id,
            "doc_type": doc_type,
            "status": "cleaned",
        }

    def _load_raw(self, doc: dict, objects: ObjectStore | None) -> str | None:
        """Load raw content from object storage."""
        uri = doc.get("raw_storage_uri") or ""
        if uri.startswith("minio://") and objects is not None:
            try:
                raw = objects.get(uri).decode("utf-8", errors="replace")
                log.info("Loaded raw content: doc=%s uri=%s bytes=%d",
                         doc.get("source_doc_id"), uri, len(raw))
                return raw
            except Exception as exc:
                log.error("Failed to load raw content: doc=%s uri=%s err=%s",
                          doc.get("source_doc_id"), uri, exc)
                return None
        if uri.startswith("local://") or uri.startswith("raw://"):
            return "<html>Stub HTML content for testing</html>"
        if not uri:
            log.warning("Document %s missing raw_storage_uri", doc.get("source_doc_id"))
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
        key = f"cleaned/{norm_hash}.txt"
        try:
            uri = objects.put(
                key,
                clean_text.encode("utf-8", errors="replace"),
                content_type="text/plain",
            )
            log.info("Stored cleaned text: hash=%s uri=%s bytes=%d",
                     norm_hash[:12], uri, len(clean_text))
            return uri
        except Exception as exc:
            log.error("Failed to store cleaned text: hash=%s err=%s", norm_hash[:12], exc)
            return None