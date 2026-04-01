"""Background worker to run crawler and pipeline."""

from __future__ import annotations

import json
import logging
import time

from semcore.core.context import PipelineContext

from src.app_factory import get_app
from src.config.settings import settings
from src.crawler.spider import Spider
from src.utils.health import startup_health_check
from src.utils.logging import setup_logging

log = logging.getLogger(__name__)

# Consecutive idle cycles before switching to exponential backoff
_IDLE_BACKOFF_START = 3
_IDLE_BACKOFF_MAX = 300  # cap at 5 minutes

# Retry policy for failed crawl tasks
_MAX_RETRIES = 3
_RETRY_BACKOFF_MINUTES = [5, 30, 120]  # delay before 1st, 2nd, 3rd retry


_SEED_SOURCES: list[dict] = [
    {
        "site_key": "rfc-editor",
        "site_name": "RFC Editor",
        "home_url": "https://www.rfc-editor.org/",
        "source_rank": "S",
        "rate_limit_rps": 1.0,
        "seed_urls": [
            "https://www.rfc-editor.org/rfc/rfc793.txt",
            "https://www.rfc-editor.org/rfc/rfc8200.txt",
            "https://www.rfc-editor.org/rfc/rfc1034.txt",
            "https://www.rfc-editor.org/rfc/rfc7231.txt",
        ],
        "scope_rules": {
            "allow": [r"^https?://www\.rfc-editor\.org/rfc/"],
            "deny": [],
        },
        "extra_headers": None,
    },
    {
        "site_key": "3gpp",
        "site_name": "3GPP",
        "home_url": "https://www.3gpp.org/",
        "source_rank": "S",
        "rate_limit_rps": 1.0,
        "seed_urls": [
            "https://portal.3gpp.org/desktopmodules/Specifications/SpecificationDetails.aspx?specificationId=3144"
        ],
        "scope_rules": {
            "allow": [r"^https?://portal\.3gpp\.org/"],
            "deny": [],
        },
        "extra_headers": None,
    },
    {
        "site_key": "itu-t",
        "site_name": "ITU-T Recommendations",
        "home_url": "https://www.itu.int/en/ITU-T/publications/pages/recs.aspx",
        "source_rank": "S",
        "rate_limit_rps": 1.0,
        "seed_urls": [
            "https://www.itu.int/net/ITU-T/lists/standards.aspx"
        ],
        "scope_rules": {
            "allow": [r"^https?://www\.itu\.int/"],
            "deny": [],
        },
        "extra_headers": None,
    },
]


def _jsonb(value: object | None) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=True)


def _auto_enqueue_seeds(store) -> None:
    total_urls = 0
    for src in _SEED_SOURCES:
        store.execute(
            """
            INSERT INTO source_registry (
                site_key, site_name, home_url, source_rank, crawl_enabled,
                rate_limit_rps, seed_urls, scope_rules, extra_headers, updated_at
            ) VALUES (
                %s, %s, %s, %s, true,
                %s, %s::jsonb, %s::jsonb, %s::jsonb, NOW()
            )
            ON CONFLICT (site_key) DO UPDATE SET
                site_name = EXCLUDED.site_name,
                home_url = EXCLUDED.home_url,
                source_rank = EXCLUDED.source_rank,
                crawl_enabled = true,
                rate_limit_rps = EXCLUDED.rate_limit_rps,
                seed_urls = EXCLUDED.seed_urls,
                scope_rules = EXCLUDED.scope_rules,
                extra_headers = EXCLUDED.extra_headers,
                updated_at = NOW()
            """,
            (
                src["site_key"],
                src["site_name"],
                src["home_url"],
                src["source_rank"],
                src["rate_limit_rps"],
                _jsonb(src.get("seed_urls")),
                _jsonb(src.get("scope_rules")),
                _jsonb(src.get("extra_headers")),
            ),
        )

        for url in src["seed_urls"]:
            store.execute(
                """
                INSERT INTO crawl_tasks (
                    site_key, url, task_type, priority, status, scheduled_at
                ) VALUES (
                    %s, %s, 'full', %s, 'pending', NOW()
                )
                ON CONFLICT (url) DO UPDATE SET
                    site_key = EXCLUDED.site_key,
                    task_type = EXCLUDED.task_type,
                    priority = EXCLUDED.priority,
                    status = 'pending',
                    scheduled_at = NOW(),
                    started_at = NULL,
                    finished_at = NULL,
                    retry_count = 0,
                    http_status = NULL,
                    error_msg = NULL,
                    raw_storage_uri = NULL,
                    content_hash = NULL,
                    parent_task_id = NULL
                """,
                (src["site_key"], url, 10),
            )
        total_urls += len(src["seed_urls"])

    log.info(
        "Auto-enqueued %d seed URLs across %d sources",
        total_urls,
        len(_SEED_SOURCES),
    )


def _retry_failed_tasks(store) -> int:
    """Re-queue failed tasks that haven't exceeded max retries and whose backoff has elapsed."""
    retried = 0
    for attempt, delay_min in enumerate(_RETRY_BACKOFF_MINUTES):
        rows = store.fetchall(
            """
            SELECT id, url, retry_count
            FROM crawl_tasks
            WHERE status = 'failed'
              AND retry_count = %s
              AND finished_at < NOW() - INTERVAL '%s minutes'
            ORDER BY priority DESC, id ASC
            LIMIT 20
            """,
            (attempt, delay_min),
        )
        for row in rows:
            store.execute(
                """
                UPDATE crawl_tasks
                SET status = 'pending',
                    scheduled_at = NOW(),
                    retry_count = retry_count + 1,
                    started_at = NULL,
                    finished_at = NULL,
                    error_msg = NULL
                WHERE id = %s
                """,
                (row["id"],),
            )
            retried += 1
            log.info(
                "Retrying failed task id=%s url=%s (attempt %d/%d)",
                row["id"], row["url"], row["retry_count"] + 1, _MAX_RETRIES,
            )
    return retried


def _fetch_pipeline_tasks(knowledge_store, limit: int) -> list[str]:
    """Find documents in 'raw' status ready for pipeline processing."""
    rows = knowledge_store.fetchall(
        """
        SELECT source_doc_id FROM documents
        WHERE status = 'raw'
        ORDER BY created_at ASC
        LIMIT %s
        """,
        (limit,),
    )
    return [str(row["source_doc_id"]) for row in rows]


def _run_pipeline(app, doc_ids: list[str]) -> None:
    for doc_id in doc_ids:
        ctx = PipelineContext(source_doc_id=doc_id)
        try:
            app.ingest_context(ctx)
            log.info("Pipeline completed for doc=%s errors=%d", doc_id, len(ctx.errors))
        except Exception as exc:
            log.error("Pipeline failed for doc=%s err=%s", doc_id, exc, exc_info=True)


def main() -> None:
    setup_logging(settings.LOG_LEVEL)
    if not startup_health_check():
        raise SystemExit("Startup health check failed.")

    app = get_app()
    crawler_store = app.crawler_store or app.store
    _auto_enqueue_seeds(crawler_store)
    spider = Spider(object_store=app.objects, store=crawler_store, knowledge_store=app.store)
    log.info(
        "Worker started: crawl_limit=%d pipeline_limit=%d sleep=%ds",
        settings.WORKER_CRAWL_LIMIT,
        settings.WORKER_PIPELINE_LIMIT,
        settings.WORKER_SLEEP_SECS,
    )

    idle_count = 0
    try:
        while True:
            try:
                # Retry failed tasks that are ready for another attempt
                retried = _retry_failed_tasks(crawler_store)

                crawl_results = spider.run_pending_tasks(limit=settings.WORKER_CRAWL_LIMIT)

                # Pipeline picks up all documents in 'raw' status (from any source)
                doc_ids = _fetch_pipeline_tasks(app.store, settings.WORKER_PIPELINE_LIMIT)
                if doc_ids:
                    _run_pipeline(app, doc_ids)

                has_work = len(crawl_results) > 0 or len(doc_ids) > 0 or retried > 0
                if has_work:
                    idle_count = 0
                    log.info(
                        "Worker cycle done: crawled=%d pipeline_docs=%d retried=%d",
                        len(crawl_results),
                        len(doc_ids),
                        retried,
                    )
                else:
                    idle_count += 1
                    if idle_count <= _IDLE_BACKOFF_START:
                        log.info("Worker cycle done: crawled=0 pipeline_tasks=0")
                    else:
                        log.debug("Worker idle (cycle %d)", idle_count)
            except Exception as exc:
                log.error("Worker cycle error: %s", exc, exc_info=True)
                idle_count = 0  # reset on error so next cycle logs at INFO

            # Exponential backoff when idle
            if idle_count > _IDLE_BACKOFF_START:
                backoff = min(
                    settings.WORKER_SLEEP_SECS * (2 ** (idle_count - _IDLE_BACKOFF_START)),
                    _IDLE_BACKOFF_MAX,
                )
                time.sleep(backoff)
            else:
                time.sleep(settings.WORKER_SLEEP_SECS)
    finally:
        spider.close()


if __name__ == "__main__":
    main()
