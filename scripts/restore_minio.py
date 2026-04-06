"""
Restore MinIO files from source URLs and PG segment data.

MinIO was wiped on Docker restart; this script re-populates it while
keeping all existing PG/Neo4j indices intact (no DB writes).

Strategy
--------
- raw/  : re-download from documents.source_url, upload to original key
          (key is taken verbatim from raw_storage_uri — no re-hashing)
- cleaned/: reconstruct by concatenating segments.raw_text in index order,
            upload to original key from cleaned_storage_uri
- Idempotent: skips objects that already exist in MinIO
- Failures are logged and skipped — never abort the whole run

Usage
-----
    python scripts/restore_minio.py               # restore all
    python scripts/restore_minio.py --raw-only    # only raw HTML files
    python scripts/restore_minio.py --clean-only  # only cleaned text files
    python scripts/restore_minio.py --workers 4   # parallel downloads
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import psycopg2
import urllib.request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "semcore"))
sys.path.insert(0, str(PROJECT_ROOT))

from src.config.settings import settings
from minio import Minio
from minio.error import S3Error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(str(PROJECT_ROOT / "logs" / "restore_minio.log"), encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# Delay between HTTP downloads (seconds) to be polite to IETF servers
DOWNLOAD_DELAY = 1.2
HTTP_TIMEOUT = 30
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TelecomKB-Restore/1.0; research)"
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def parse_minio_uri(uri: str) -> tuple[str, str]:
    """Parse 'minio://bucket/key' → (bucket, key)."""
    parsed = urlparse(uri)
    return parsed.netloc, parsed.path.lstrip("/")


def get_minio_client() -> Minio:
    return Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=False,
    )


def object_exists(client: Minio, bucket: str, key: str) -> bool:
    try:
        client.stat_object(bucket, key)
        return True
    except S3Error as e:
        if e.code == "NoSuchKey":
            return False
        raise


def ensure_bucket(client: Minio, bucket: str) -> None:
    if not client.bucket_exists(bucket):
        client.make_bucket(bucket)
        log.info("Created bucket: %s", bucket)


def download_url(url: str) -> bytes | None:
    """Download URL, return bytes or None on failure."""
    try:
        req = urllib.request.Request(url, headers=HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return resp.read()
    except Exception as exc:
        log.warning("  Download failed: %s — %s", url, exc)
        return None


def upload_bytes(client: Minio, bucket: str, key: str, data: bytes, content_type: str) -> bool:
    import io
    try:
        client.put_object(bucket, key, io.BytesIO(data), length=len(data), content_type=content_type)
        return True
    except Exception as exc:
        log.warning("  Upload failed: %s/%s — %s", bucket, key, exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Raw file restoration
# ─────────────────────────────────────────────────────────────────────────────

def load_raw_docs(conn) -> list[dict]:
    """Load all documents that need raw file restoration."""
    cur = conn.cursor()
    cur.execute("""
        SELECT source_doc_id, source_url, raw_storage_uri
        FROM documents
        WHERE raw_storage_uri IS NOT NULL
          AND source_url IS NOT NULL AND source_url != ''
        ORDER BY source_doc_id
    """)
    rows = cur.fetchall()
    return [{"source_doc_id": r[0], "source_url": r[1], "raw_uri": r[2]} for r in rows]


def restore_raw_one(doc: dict, client: Minio, delay: float) -> str:
    """
    Restore one raw file. Returns 'skipped', 'ok', or 'failed'.
    """
    bucket, key = parse_minio_uri(doc["raw_uri"])
    ensure_bucket(client, bucket)

    if object_exists(client, bucket, key):
        return "skipped"

    time.sleep(delay)
    data = download_url(doc["source_url"])
    if data is None:
        log.warning("FAIL raw  %s → %s", doc["source_doc_id"], doc["source_url"])
        return "failed"

    ok = upload_bytes(client, bucket, key, data, "text/html; charset=utf-8")
    if ok:
        log.info("OK   raw  %s (%d bytes)", key[:20] + "...", len(data))
        return "ok"
    return "failed"


def restore_raw(conn, workers: int = 1) -> dict:
    docs = load_raw_docs(conn)
    log.info("Raw files to check: %d", len(docs))
    counts = {"ok": 0, "skipped": 0, "failed": 0}

    # Sequential if workers=1 (safer for rate limiting), parallel otherwise
    if workers <= 1:
        client = get_minio_client()
        for i, doc in enumerate(docs, 1):
            result = restore_raw_one(doc, client, DOWNLOAD_DELAY)
            counts[result] += 1
            if i % 50 == 0:
                log.info("Progress raw: %d/%d — ok=%d skip=%d fail=%d",
                         i, len(docs), counts["ok"], counts["skipped"], counts["failed"])
    else:
        # Each thread gets its own MinIO client
        def worker_fn(doc):
            c = get_minio_client()
            return restore_raw_one(doc, c, DOWNLOAD_DELAY * workers)  # scale delay

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(worker_fn, doc): doc for doc in docs}
            done = 0
            for fut in as_completed(futures):
                result = fut.result()
                counts[result] += 1
                done += 1
                if done % 50 == 0:
                    log.info("Progress raw: %d/%d — ok=%d skip=%d fail=%d",
                             done, len(docs), counts["ok"], counts["skipped"], counts["failed"])

    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Cleaned file restoration (from PG segments)
# ─────────────────────────────────────────────────────────────────────────────

def load_clean_docs(conn) -> list[dict]:
    """Load documents that have a cleaned_storage_uri."""
    cur = conn.cursor()
    cur.execute("""
        SELECT source_doc_id, cleaned_storage_uri
        FROM documents
        WHERE cleaned_storage_uri IS NOT NULL
        ORDER BY source_doc_id
    """)
    rows = cur.fetchall()
    return [{"source_doc_id": r[0], "cleaned_uri": r[1]} for r in rows]


def load_segments_for_doc(conn, source_doc_id: str) -> list[str]:
    """Return raw_text of all segments for a doc in index order."""
    cur = conn.cursor()
    cur.execute("""
        SELECT raw_text
        FROM segments
        WHERE source_doc_id = %s
          AND lifecycle_state = 'active'
          AND raw_text IS NOT NULL
        ORDER BY segment_index, id
    """, (source_doc_id,))
    return [r[0] for r in cur.fetchall()]


def restore_cleaned_one(doc: dict, conn, client: Minio) -> str:
    bucket, key = parse_minio_uri(doc["cleaned_uri"])
    ensure_bucket(client, bucket)

    if object_exists(client, bucket, key):
        return "skipped"

    texts = load_segments_for_doc(conn, doc["source_doc_id"])
    if not texts:
        log.warning("FAIL clean %s — no segments found", doc["source_doc_id"])
        return "failed"

    content = "\n\n".join(texts)
    data = content.encode("utf-8")
    ok = upload_bytes(client, bucket, key, data, "text/plain; charset=utf-8")
    if ok:
        log.info("OK   clean %s (%d segs, %d bytes)", key[:20] + "...", len(texts), len(data))
        return "ok"
    return "failed"


def restore_cleaned(conn) -> dict:
    docs = load_clean_docs(conn)
    log.info("Cleaned files to check: %d", len(docs))
    client = get_minio_client()
    counts = {"ok": 0, "skipped": 0, "failed": 0}

    for i, doc in enumerate(docs, 1):
        result = restore_cleaned_one(doc, conn, client)
        counts[result] += 1
        if i % 100 == 0:
            log.info("Progress clean: %d/%d — ok=%d skip=%d fail=%d",
                     i, len(docs), counts["ok"], counts["skipped"], counts["failed"])

    return counts


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Restore MinIO files from PG/source URLs")
    parser.add_argument("--raw-only", action="store_true", help="Only restore raw HTML files")
    parser.add_argument("--clean-only", action="store_true", help="Only restore cleaned text files")
    parser.add_argument("--workers", type=int, default=1,
                        help="Parallel download threads for raw files (default: 1)")
    args = parser.parse_args()

    conn = psycopg2.connect(
        host=settings.POSTGRES_HOST,
        port=settings.POSTGRES_PORT,
        dbname=settings.POSTGRES_DB,
        user=settings.POSTGRES_USER,
        password=settings.POSTGRES_PASSWORD,
    )

    try:
        do_raw = not args.clean_only
        do_clean = not args.raw_only

        if do_raw:
            log.info("=== Restoring raw files (re-download from source URLs) ===")
            raw_counts = restore_raw(conn, workers=args.workers)
            log.info("Raw done: ok=%d skipped=%d failed=%d",
                     raw_counts["ok"], raw_counts["skipped"], raw_counts["failed"])

        if do_clean:
            log.info("=== Restoring cleaned files (reconstruct from PG segments) ===")
            clean_counts = restore_cleaned(conn)
            log.info("Cleaned done: ok=%d skipped=%d failed=%d",
                     clean_counts["ok"], clean_counts["skipped"], clean_counts["failed"])

    finally:
        conn.close()

    log.info("Restore complete.")


if __name__ == "__main__":
    main()