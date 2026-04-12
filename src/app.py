"""
FastAPI application entry point.

Run:
    uvicorn src.app:app --host 0.0.0.0 --port 8001 --reload
"""

from __future__ import annotations

import logging

# Use Windows system certificate store (needed for DeepSeek API and similar services)
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from src.config.settings import settings
from src.db import health_check
from src.utils.health import startup_health_check
from src.api.semantic.router import router as semantic_router
from src.api.system.router import router as system_router
from src.utils.logging import setup_logging

app = FastAPI(
    title="Telecom Semantic KB API",
    version="0.1.0",
    description="Semantic knowledge base for network & telecom domain.",
    docs_url="/docs",
    redoc_url="/redoc",
)

log = logging.getLogger(__name__)


@app.on_event("startup")
async def on_startup() -> None:
    setup_logging(settings.LOG_LEVEL)
    if not startup_health_check():
        log.error("Startup health check failed.")
        if settings.STARTUP_HEALTH_REQUIRED:
            log.error("Shutting down.")
            raise RuntimeError("Startup health check failed.")
        log.warning(
            "Continuing startup with degraded health (STARTUP_HEALTH_REQUIRED=false)."
        )

    # Stats scheduler is now managed by the worker process (stats thread).
    # API only serves the dashboard — no duplicate scheduler here.


# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(semantic_router)
app.include_router(system_router)

# ── Static files (dashboard) ─────────────────────────────────────────────────
_static_dir = Path(__file__).resolve().parents[1] / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


# ── Utility endpoints ─────────────────────────────────────────────────────────

@app.get("/health", tags=["ops"])
def health() -> dict:
    status = health_check()
    status["status"] = "ok" if all(status.values()) else "degraded"
    return status


@app.get("/dashboard", include_in_schema=False)
def dashboard():
    return RedirectResponse(url="/static/dashboard.html")


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")
