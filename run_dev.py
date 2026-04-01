"""
Dev server — runs with in-memory SQLite + dict stores, no external services needed.

    python run_dev.py
    # or
    uvicorn run_dev:app --reload

Endpoints to smoke-test:
    GET  /health
    GET  /api/v1/semantic/lookup?term=BGP
    GET  /api/v1/semantic/resolve?alias=border+gateway+protocol
    GET  /docs
"""

from __future__ import annotations

import sys
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(name)s  %(message)s")

# Make the semcore package importable without installing it
_semcore_path = str(Path(__file__).parent / "semcore")
if _semcore_path not in sys.path:
    sys.path.insert(0, _semcore_path)

# ── Step 1: inject fake db modules BEFORE any src.db import ──────────────────
# Must patch src.db package itself AND its submodules so that both
# `from src.db.postgres import ...` and `import src.db.postgres as pg` work.
import types
from src.dev import fake_postgres, fake_neo4j, fake_crawler_postgres

_db_mod = types.ModuleType("src.db")
_db_mod.postgres = fake_postgres        # type: ignore[attr-defined]
_db_mod.neo4j_client = fake_neo4j       # type: ignore[attr-defined]
_db_mod.crawler_postgres = fake_crawler_postgres  # type: ignore[attr-defined]
_db_mod.health_check = lambda: {"postgres": True, "neo4j": True, "crawler_postgres": True}  # type: ignore[attr-defined]

sys.modules["src.db"]                  = _db_mod                # type: ignore[assignment]
sys.modules["src.db.postgres"]         = fake_postgres          # type: ignore[assignment]
sys.modules["src.db.neo4j_client"]     = fake_neo4j             # type: ignore[assignment]
sys.modules["src.db.crawler_postgres"] = fake_crawler_postgres  # type: ignore[assignment]

# ── Step 2: seed in-memory stores from YAML ontology ─────────────────────────
from src.dev.seed import seed_from_registry
seed_from_registry()

# ── Step 3: import the real FastAPI app (all src.* imports happen now) ────────
from src.app import app   # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("run_dev:app", host="127.0.0.1", port=8000, reload=False)