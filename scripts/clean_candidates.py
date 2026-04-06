"""CLI wrapper for ontology maintenance — manual trigger.

Usage:
    python scripts/clean_candidates.py [--skip-embedding] [--skip-llm]

Calls src/governance/maintenance.py which is also used by the worker maintenance thread.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "semcore"))
sys.path.insert(0, str(PROJECT_ROOT))

# HuggingFace mirror
if not os.environ.get("HF_ENDPOINT"):
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["EMBEDDING_ENABLED"] = "true"

import logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")


def main():
    skip_embedding = "--skip-embedding" in sys.argv
    skip_llm = "--skip-llm" in sys.argv

    from src.app_factory import get_app
    app = get_app()

    from src.governance.maintenance import OntologyMaintenance
    maint = OntologyMaintenance(store=app.store, graph=app.graph, ontology=app.ontology)
    stats = maint.run(skip_embedding=skip_embedding, skip_llm=skip_llm)

    print(f"\nDone: {stats}")


if __name__ == "__main__":
    main()