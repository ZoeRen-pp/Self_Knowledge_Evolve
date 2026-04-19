"""Startup health checks for all external dependencies."""

from __future__ import annotations

import logging

from src.db import health_check as db_health_check

log = logging.getLogger(__name__)


def startup_health_check() -> bool:
    """Check all dependencies before starting services.

    Required: postgres, neo4j, crawler_postgres, minio
    Required if enabled: llm, embedding
    Returns True only if all required services are healthy.
    """
    from src.config.settings import settings

    results: dict[str, bool | str] = {}

    # ── Databases ──
    db_status = db_health_check()
    results["postgres"] = db_status.get("postgres", False)
    results["neo4j"] = db_status.get("neo4j", False)
    results["crawler_postgres"] = db_status.get("crawler_postgres", False)

    # ── MinIO ──
    try:
        from minio import Minio
        mc = Minio(
            settings.MINIO_ENDPOINT,
            access_key=settings.MINIO_ACCESS_KEY,
            secret_key=settings.MINIO_SECRET_KEY,
            secure=settings.MINIO_SECURE,
        )
        mc.bucket_exists(settings.MINIO_BUCKET_RAW)
        results["minio"] = True
    except Exception as exc:
        log.error("MinIO health check failed: %s", exc)
        results["minio"] = False

    # ── LLM ──
    if settings.LLM_ENABLED:
        from src.utils.llm_extract import LLMExtractor
        llm_ok = LLMExtractor().ping()
        results["llm"] = llm_ok
    else:
        results["llm"] = "disabled"

    # ── Embedding ──
    if getattr(settings, "EMBEDDING_ENABLED", False):
        emb_ok = _check_embedding()
        results["embedding"] = emb_ok
    else:
        results["embedding"] = "disabled"

    # ── Report ──
    failed = [k for k, v in results.items() if v is False]
    disabled = [k for k, v in results.items() if v == "disabled"]
    passed = [k for k, v in results.items() if v is True]

    if not failed:
        log.info("Startup health check ok: %s",
                 ", ".join(f"{k}=ok" for k in passed) +
                 (f" | disabled: {', '.join(disabled)}" if disabled else ""))
        return True
    else:
        log.error("Startup health check FAILED: %s",
                  ", ".join(f"{k}=FAIL" for k in failed) +
                  " | " + ", ".join(f"{k}=ok" for k in passed) +
                  (f" | disabled: {', '.join(disabled)}" if disabled else ""))
        return False


def _check_embedding() -> bool:
    """Check if at least one embedding backend is reachable."""
    from src.utils.embedding import _detect_backend
    backend = _detect_backend()
    if backend == "disabled":
        return False

    try:
        from src.utils.embedding import get_embeddings
        vecs = get_embeddings(["health check"])
        if vecs and len(vecs) == 1 and len(vecs[0]) > 0:
            log.info("Embedding health check ok (backend=%s, dim=%d)", backend, len(vecs[0]))
            return True
        log.error("Embedding returned empty result (backend=%s)", backend)
        return False
    except Exception as exc:
        log.error("Embedding health check failed (backend=%s): %s", backend, exc)
        return False