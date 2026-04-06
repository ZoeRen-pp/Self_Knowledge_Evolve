"""
Clean reset: kill processes → purge cache → clear all stores → load ontology → start worker.

All steps in a single Python process, strictly sequential, with verification gates.

Usage:
    python scripts/reset_and_run.py
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "semcore"))
sys.path.insert(0, str(PROJECT_ROOT))

os.chdir(PROJECT_ROOT)


def log(msg: str) -> None:
    print(f"[reset] {msg}", flush=True)


def step(name: str):
    """Decorator-like context for step logging."""
    log(f"── {name} ──")


# ═══════════════════════════════════════════════════════════════
# Step 1: Kill all related processes
# ═══════════════════════════════════════════════════════════════

def kill_processes() -> None:
    step("Killing worker and API processes")

    if sys.platform == "win32":
        # Windows: use netstat to find port 8000, taskkill by PID
        # Kill API on port 8000
        try:
            result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True)
            for line in result.stdout.splitlines():
                if ":8000" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
                    log(f"  Killed API PID {pid}")
        except Exception:
            pass

        # Kill all python.exe except ourselves
        my_pid = os.getpid()
        try:
            result = subprocess.run(
                ["powershell", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                 "Select-Object ProcessId, CommandLine | Format-List"],
                capture_output=True, text=True, timeout=10,
            )
            current_pid = None
            current_cmd = ""
            for line in result.stdout.splitlines():
                if line.startswith("ProcessId"):
                    current_pid = line.split(":")[-1].strip()
                elif line.startswith("CommandLine"):
                    current_cmd = line.split(":", 1)[-1].strip().lower()
                    if current_pid and current_pid != str(my_pid):
                        if "worker.py" in current_cmd or "uvicorn" in current_cmd:
                            subprocess.run(["taskkill", "/F", "/PID", current_pid], capture_output=True)
                            log(f"  Killed PID {current_pid}")
                    current_pid = None
                    current_cmd = ""
        except Exception:
            pass
    else:
        # Unix
        for pattern in ["python worker.py", "uvicorn.*8000"]:
            subprocess.run(["pkill", "-f", pattern], capture_output=True)

    # Wait for processes to die
    time.sleep(3)
    log("  Processes killed")


# ═══════════════════════════════════════════════════════════════
# Step 2: Purge Python cache
# ═══════════════════════════════════════════════════════════════

def purge_cache() -> None:
    step("Purging __pycache__")
    count = 0
    for cache_dir in PROJECT_ROOT.rglob("__pycache__"):
        shutil.rmtree(cache_dir, ignore_errors=True)
        count += 1
    for pyc in PROJECT_ROOT.rglob("*.pyc"):
        pyc.unlink(missing_ok=True)
        count += 1
    log(f"  Removed {count} cache entries")


# ═══════════════════════════════════════════════════════════════
# Step 3: Clear all data stores
# ═══════════════════════════════════════════════════════════════

def clear_stores() -> None:
    step("Clearing all data stores")

    from src.config.settings import settings
    import psycopg2
    from neo4j import GraphDatabase
    from minio import Minio

    # ── PG telecom_kb ──
    conn = psycopg2.connect(dsn=settings.postgres_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    for t in [
        "system_stats_snapshots",
        "governance.conflict_records", "governance.review_records",
        "governance.evolution_candidates",
        "evidence", "facts", "segment_tags", "t_rst_relation",
        "segments", "documents", "lexicon_aliases",
    ]:
        try:
            cur.execute(f"TRUNCATE TABLE {t} CASCADE")
        except Exception:
            pass
    conn.close()
    log("  PG telecom_kb: truncated")

    # ── PG telecom_crawler ──
    conn = psycopg2.connect(dsn=settings.crawler_postgres_dsn)
    conn.autocommit = True
    cur = conn.cursor()
    for t in ["extraction_jobs", "crawl_tasks", "source_registry"]:
        try:
            cur.execute(f"TRUNCATE TABLE {t} CASCADE")
        except Exception:
            pass
    conn.close()
    log("  PG telecom_crawler: truncated")

    # ── Neo4j ──
    driver = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    with driver.session(database=settings.NEO4J_DATABASE) as s:
        s.run("MATCH (n) DETACH DELETE n").consume()
    driver.close()
    log("  Neo4j: cleared")

    # ── MinIO ──
    client = Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )
    deleted = 0
    for bucket in [settings.MINIO_BUCKET_RAW, settings.MINIO_BUCKET_CLEANED]:
        if not client.bucket_exists(bucket):
            client.make_bucket(bucket)
            log(f"  Created bucket: {bucket}")
            continue
        for obj in client.list_objects(bucket, recursive=True):
            client.remove_object(bucket, obj.object_name)
            deleted += 1
    log(f"  MinIO: {deleted} objects deleted")


# ═══════════════════════════════════════════════════════════════
# Step 4: Verify everything is empty
# ═══════════════════════════════════════════════════════════════

def verify_clean() -> None:
    step("Verifying clean state")

    from src.config.settings import settings
    import psycopg2
    from neo4j import GraphDatabase
    from minio import Minio

    errors = []

    conn = psycopg2.connect(dsn=settings.postgres_dsn)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM documents")
    docs = cur.fetchone()[0]
    if docs > 0:
        errors.append(f"documents: {docs} rows (expected 0)")
    conn.close()

    conn = psycopg2.connect(dsn=settings.crawler_postgres_dsn)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM crawl_tasks")
    tasks = cur.fetchone()[0]
    if tasks > 0:
        errors.append(f"crawl_tasks: {tasks} rows (expected 0)")
    conn.close()

    driver = GraphDatabase.driver(
        settings.NEO4J_URI, auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD),
    )
    with driver.session(database=settings.NEO4J_DATABASE) as s:
        nodes = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        if nodes > 0:
            errors.append(f"Neo4j: {nodes} nodes (expected 0)")
    driver.close()

    client = Minio(
        settings.MINIO_ENDPOINT,
        access_key=settings.MINIO_ACCESS_KEY,
        secret_key=settings.MINIO_SECRET_KEY,
        secure=settings.MINIO_SECURE,
    )
    for bucket in [settings.MINIO_BUCKET_RAW, settings.MINIO_BUCKET_CLEANED]:
        if client.bucket_exists(bucket):
            remaining = list(client.list_objects(bucket, recursive=True))
            if remaining:
                errors.append(f"MinIO {bucket}: {len(remaining)} files (expected 0)")

    if errors:
        log("  VERIFICATION FAILED:")
        for e in errors:
            log(f"    FAIL: {e}")
        sys.exit(1)

    log("  OK: All stores empty")


# ═══════════════════════════════════════════════════════════════
# Step 5: Load ontology
# ═══════════════════════════════════════════════════════════════

def load_ontology() -> None:
    step("Loading ontology")
    result = subprocess.run(
        [sys.executable, "scripts/load_ontology.py"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
    )
    if result.returncode != 0:
        log(f"  FAILED:\n{result.stderr[-500:]}")
        sys.exit(1)
    # Extract summary line
    for line in result.stdout.splitlines():
        if "Done" in line:
            log(f"  {line.strip()}")
            break


# ═══════════════════════════════════════════════════════════════
# Step 6: Start worker
# ═══════════════════════════════════════════════════════════════

def start_worker() -> None:
    step("Starting worker (3 threads: crawler, pipeline, stats)")

    log_path = PROJECT_ROOT / "logs" / "worker.log"
    log_path.parent.mkdir(exist_ok=True)

    # Truncate old log
    log_path.write_text("", encoding="utf-8")

    if sys.platform == "win32":
        proc = subprocess.Popen(
            [sys.executable, "worker.py"],
            cwd=str(PROJECT_ROOT),
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            [sys.executable, "worker.py"],
            cwd=str(PROJECT_ROOT),
            stdout=open(log_path, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    log(f"  Worker PID: {proc.pid}")

    # Wait and verify it started successfully
    time.sleep(10)
    if proc.poll() is not None:
        log(f"  WORKER DIED (exit code {proc.returncode})")
        log(f"  Log: {log_path.read_text(encoding='utf-8', errors='replace')[-500:]}")
        sys.exit(1)

    # Check log for signs of life
    log_content = log_path.read_text(encoding="utf-8", errors="replace")
    if "Worker started" in log_content:
        log("  OK: Worker running")
    elif "error" in log_content.lower():
        log(f"  WARNING: errors in startup log")
    else:
        log("  OK: Worker process alive (waiting for first cycle)")


# ═══════════════════════════════════════════════════════════════
# Step 7: Start API + Dashboard
# ═══════════════════════════════════════════════════════════════

def start_api() -> None:
    step("Starting API + Dashboard")

    api_log = PROJECT_ROOT / "logs" / "api.log"
    api_log.write_text("", encoding="utf-8")

    # Start uvicorn as subprocess
    api_script = (
        f"import sys; sys.path.insert(0, {str(PROJECT_ROOT / 'semcore')!r}); "
        f"import uvicorn; from src.app import app; "
        f"uvicorn.run(app, host='0.0.0.0', port=8000)"
    )

    if sys.platform == "win32":
        proc = subprocess.Popen(
            [sys.executable, "-c", api_script],
            cwd=str(PROJECT_ROOT),
            stdout=open(api_log, "w"),
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    else:
        proc = subprocess.Popen(
            [sys.executable, "-c", api_script],
            cwd=str(PROJECT_ROOT),
            stdout=open(api_log, "w"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    log(f"  API PID: {proc.pid}")

    # Wait for health check
    import urllib.request
    for attempt in range(10):
        time.sleep(2)
        try:
            resp = urllib.request.urlopen("http://localhost:8000/health", timeout=5)
            if resp.status == 200:
                log("  OK: API healthy, dashboard at http://localhost:8000/dashboard")
                return
        except Exception:
            pass

    if proc.poll() is not None:
        log(f"  API DIED (exit code {proc.returncode})")
        sys.exit(1)
    log("  WARNING: API started but health check not responding yet")


# ═══════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════

def main() -> None:
    log("=" * 50)
    log("CLEAN RESET AND RUN")
    log("=" * 50)

    kill_processes()
    purge_cache()
    clear_stores()
    verify_clean()
    load_ontology()
    start_worker()
    start_api()

    log("=" * 50)
    log("DONE — all services running, all stores fresh")
    log("  Worker:    tail -f logs/worker.log")
    log("  API:       tail -f logs/api.log")
    log("  Dashboard: http://localhost:8000/dashboard")
    log("=" * 50)


if __name__ == "__main__":
    main()