"""
Embedding client — HTTP BGE-M3 service (preferred) or Ollama or sentence-transformers.

Priority:
1. HTTP BGE-M3 service at EMBEDDING_HTTP_URL (default http://localhost:8000) —
   POST /embedding {"text": "..."} → {"embedding": [1024 floats]}. This is the
   BGE-M3 systemd service running in WSL2 (see CLAUDE.md).
2. Ollama API at OLLAMA_URL (default http://localhost:11434) — only used if the
   HTTP service is down; requires `ollama pull bge-m3` (not installed by default).
3. sentence-transformers with BAAI/bge-m3 — local Python model fallback.

Usage:
    from src.utils.embedding import get_embeddings
    vecs = get_embeddings(["BGP best path selection", "BGP最优路径选择"])
    # → list of 1024-dim float lists, or None if disabled/unavailable
"""

from __future__ import annotations

import json
import logging
import urllib.request
from typing import Optional

log = logging.getLogger(__name__)

_backend: Optional[str] = None   # 'http' | 'ollama' | 'st' | 'disabled'
_st_model = None                 # lazy-loaded SentenceTransformer


def _is_enabled() -> bool:
    from src.config.settings import settings
    return getattr(settings, "EMBEDDING_ENABLED", False)


def _detect_backend() -> str:
    """Auto-detect best available embedding backend."""
    global _backend
    if _backend is not None:
        return _backend

    if not _is_enabled():
        _backend = "disabled"
        return _backend

    from src.config.settings import settings

    # 1. HTTP BGE-M3 service — preferred (the WSL2 systemd service on :8000)
    http_url = getattr(settings, "EMBEDDING_HTTP_URL", "http://localhost:8000")
    try:
        req = urllib.request.Request(
            f"{http_url}/embedding",
            data=json.dumps({"text": "test"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        vec = data.get("embedding") if isinstance(data, dict) else None
        if vec and len(vec) > 0:
            _backend = "http"
            log.info("Embedding backend: HTTP BGE-M3 at %s (dim=%d)", http_url, len(vec))
            return _backend
    except Exception as exc:
        log.debug("HTTP BGE-M3 service not available at %s: %s", http_url, exc)

    # 2. Ollama API
    ollama_url = getattr(settings, "OLLAMA_URL", "http://localhost:11434")
    ollama_model = getattr(settings, "OLLAMA_EMBED_MODEL", "bge-m3")
    try:
        req = urllib.request.Request(
            f"{ollama_url}/api/embed",
            data=json.dumps({"model": ollama_model, "input": ["test"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        if data.get("embeddings") and len(data["embeddings"][0]) > 0:
            _backend = "ollama"
            log.info("Embedding backend: Ollama (%s, dim=%d)", ollama_model, len(data["embeddings"][0]))
            return _backend
    except Exception as exc:
        log.debug("Ollama not available: %s", exc)

    # 3. sentence-transformers
    try:
        from sentence_transformers import SentenceTransformer
        _backend = "st"
        log.info("Embedding backend: sentence-transformers")
        return _backend
    except ImportError:
        pass

    log.warning(
        "No embedding backend available "
        "(HTTP %s / Ollama / sentence-transformers all unavailable)",
        http_url,
    )
    _backend = "disabled"
    return _backend


def _get_st_model():
    """Lazy-load sentence-transformers model."""
    global _st_model
    if _st_model is not None:
        return _st_model
    from src.config.settings import settings
    try:
        from sentence_transformers import SentenceTransformer
        log.info("Loading embedding model %s on %s ...", settings.EMBEDDING_MODEL, settings.EMBEDDING_DEVICE)
        _st_model = SentenceTransformer(settings.EMBEDDING_MODEL, device=settings.EMBEDDING_DEVICE)
        log.info("Embedding model loaded (dim=%d)", settings.EMBEDDING_DIM)
    except Exception as exc:
        log.warning("Failed to load ST model: %s", exc)
    return _st_model


def get_embeddings(texts: list[str]) -> list[list[float]] | None:
    """Encode a batch of texts. Returns list of float vectors, or None if unavailable."""
    if not texts:
        return []

    backend = _detect_backend()

    if backend == "disabled":
        return None

    if backend == "http":
        return _http_embed(texts)

    if backend == "ollama":
        return _ollama_embed(texts)

    if backend == "st":
        return _st_embed(texts)

    return None


def _http_embed(texts: list[str]) -> list[list[float]] | None:
    """Embed via the HTTP BGE-M3 service.

    The service exposes POST /embedding with {"text": "..."} (single input).
    We use concurrent requests to maximize throughput while the GPU processes
    one request at a time (the server queues them internally).
    """
    from src.config.settings import settings
    from concurrent.futures import ThreadPoolExecutor, as_completed
    http_url = getattr(settings, "EMBEDDING_HTTP_URL", "http://localhost:8000")

    def _embed_one(idx_text: tuple[int, str]) -> tuple[int, list[float] | None]:
        idx, text = idx_text
        try:
            req = urllib.request.Request(
                f"{http_url}/embedding",
                data=json.dumps({"text": text or ""}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read())
            vec = data.get("embedding") if isinstance(data, dict) else None
            return (idx, vec)
        except Exception as exc:
            log.debug("HTTP embed failed for idx %d: %s", idx, exc)
            return (idx, None)

    max_workers = min(8, len(texts))
    try:
        results: list[tuple[int, list[float] | None]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_embed_one, (i, t)): i for i, t in enumerate(texts)}
            for fut in as_completed(futures):
                results.append(fut.result())
        results.sort(key=lambda x: x[0])
        vecs = []
        for idx, vec in results:
            if vec is None:
                log.warning("HTTP BGE-M3 returned no embedding for text %d", idx)
                return None
            vecs.append(vec)
        return vecs
    except Exception as exc:
        log.warning("HTTP BGE-M3 embedding failed: %s", exc)
        return None


def _ollama_embed(texts: list[str]) -> list[list[float]] | None:
    """Embed via Ollama API. Supports batching."""
    from src.config.settings import settings
    ollama_url = getattr(settings, "OLLAMA_URL", "http://localhost:11434")
    ollama_model = getattr(settings, "OLLAMA_EMBED_MODEL", "bge-m3")

    BATCH_SIZE = 64
    all_vecs = []
    try:
        for i in range(0, len(texts), BATCH_SIZE):
            batch = texts[i:i + BATCH_SIZE]
            req = urllib.request.Request(
                f"{ollama_url}/api/embed",
                data=json.dumps({"model": ollama_model, "input": batch}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            resp = urllib.request.urlopen(req, timeout=120)
            data = json.loads(resp.read())
            embeddings = data.get("embeddings", [])
            all_vecs.extend(embeddings)
        return all_vecs
    except Exception as exc:
        log.warning("Ollama embedding failed: %s", exc)
        return None


def _st_embed(texts: list[str]) -> list[list[float]] | None:
    """Embed via sentence-transformers."""
    model = _get_st_model()
    if model is None:
        return None
    from src.config.settings import settings
    try:
        vecs = model.encode(
            texts,
            batch_size=getattr(settings, "EMBEDDING_BATCH_SIZE", 32),
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() for v in vecs]
    except Exception as exc:
        log.warning("ST embedding failed: %s", exc)
        return None


def embed_query(query: str) -> list[float] | None:
    """Encode a single query string."""
    results = get_embeddings([query])
    if results:
        return results[0]
    return None


def get_embedding_model():
    """Return the ST model if available (for backward compat with stage3_align)."""
    backend = _detect_backend()
    if backend == "st":
        return _get_st_model()
    return None


def vector_to_pg_literal(vec: list[float]) -> str:
    """Format a float list as a PostgreSQL vector literal string."""
    return "[" + ",".join(f"{v:.8f}" for v in vec) + "]"