"""Reranker client — HTTP bge-reranker-v2 service on :8002.

Mirrors the embedding client pattern (src/utils/embedding.py):
POST to RERANKER_HTTP_URL with (query, passage) pairs, returns relevance scores.
Falls back gracefully when the service is unavailable.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_available: bool | None = None


def _get_url() -> str:
    from src.config.settings import settings
    return getattr(settings, "RERANKER_HTTP_URL", "http://localhost:8002")


def _is_enabled() -> bool:
    from src.config.settings import settings
    return getattr(settings, "RERANKER_ENABLED", False)


def _check_available() -> bool:
    global _available
    if _available is not None:
        return _available
    if not _is_enabled():
        _available = False
        return False
    url = _get_url()
    try:
        req = urllib.request.Request(
            f"{url}/rerank",
            data=json.dumps({"query": "test", "passages": ["test"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if isinstance(data, dict) and "scores" in data:
            _available = True
            log.info("Reranker backend: bge-reranker-v2 at %s", url)
            return True
    except Exception as exc:
        log.debug("Reranker service not available at %s: %s", url, exc)
    _available = False
    return False


def rerank_pairs(query: str, passages: list[str]) -> list[float] | None:
    """Score (query, passage) pairs via the reranker service.

    Returns a list of float scores (one per passage), or None if unavailable.
    """
    if not passages or not _check_available():
        return None
    url = _get_url()
    try:
        payload: dict[str, Any] = {"query": query, "passages": passages}
        req = urllib.request.Request(
            f"{url}/rerank",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=30)
        data = json.loads(resp.read())
        scores = data.get("scores") if isinstance(data, dict) else None
        if scores and len(scores) == len(passages):
            return [float(s) for s in scores]
        log.warning("Reranker returned unexpected shape: %s", type(data))
        return None
    except Exception as exc:
        log.warning("Reranker call failed: %s", exc)
        return None
