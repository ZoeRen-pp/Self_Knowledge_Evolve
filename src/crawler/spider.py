"""
Simplified HTTP crawler with robots.txt compliance and rate limiting.
Not a full Scrapy spider — uses httpx for fetching.
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from typing import TypedDict
from urllib.parse import urlparse

import httpx

from src.db.postgres import fetchall, execute
from src.utils.logging import get_logger

log = get_logger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "TelecomKB-Crawler/0.1 (research; contact: admin@example.com)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
}


class CrawlTask(TypedDict):
    id: int
    url: str
    site_key: str
    priority: int
    task_type: str


class Spider:
    def __init__(self) -> None:
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request_time: dict[str, float] = {}
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
        )

    # ── Public ────────────────────────────────────────────────────

    def fetch(self, url: str, extra_headers: dict | None = None) -> dict | None:
        """Fetch a URL. Returns {html, status_code, final_url, content_type} or None."""
        site_key = self._site_key_from_url(url)
        self._respect_rate_limit(site_key)
        try:
            headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
            resp = self._client.get(url, headers=headers)
            self._last_request_time[site_key] = time.monotonic()
            return {
                "html":         resp.text,
                "status_code":  resp.status_code,
                "final_url":    str(resp.url),
                "content_type": resp.headers.get("content-type", ""),
            }
        except Exception as exc:
            log.warning("Fetch failed for %s: %s", url, exc)
            return None

    def check_robots(self, site_key: str, url: str) -> bool:
        """Return True if URL is allowed by robots.txt."""
        rp = self._get_robots_parser(site_key, url)
        if rp is None:
            return True  # no robots.txt → assume allowed
        return rp.can_fetch(DEFAULT_HEADERS["User-Agent"], url)

    def run_pending_tasks(self, limit: int = 10) -> list[dict]:
        """Fetch pending crawl_tasks from DB and process each."""
        tasks = fetchall(
            """
            SELECT ct.id, ct.url, ct.site_key, ct.priority, ct.task_type,
                   sr.rate_limit_rps, sr.extra_headers
            FROM crawl_tasks ct
            JOIN source_registry sr ON ct.site_key = sr.site_key
            WHERE ct.status = 'pending' AND sr.crawl_enabled = true
            ORDER BY ct.priority DESC, ct.id ASC
            LIMIT %s
            """,
            (limit,),
        )

        results = []
        for task in tasks:
            result = self._process_task(task)
            results.append(result)
        return results

    def close(self) -> None:
        self._client.close()

    # ── Private ───────────────────────────────────────────────────

    def _process_task(self, task: dict) -> dict:
        task_id = task["id"]
        url = task["url"]

        # Mark running
        execute(
            "UPDATE crawl_tasks SET status='running', started_at=NOW() WHERE id=%s",
            (task_id,),
        )

        try:
            # Check robots
            if not self.check_robots(task["site_key"], url):
                execute(
                    "UPDATE crawl_tasks SET status='skipped', finished_at=NOW() WHERE id=%s",
                    (task_id,),
                )
                return {"task_id": task_id, "status": "skipped", "reason": "robots_disallowed"}

            result = self.fetch(url, extra_headers=task.get("extra_headers") or {})

            if result is None or result["status_code"] >= 400:
                execute(
                    "UPDATE crawl_tasks SET status='failed', http_status=%s, finished_at=NOW() WHERE id=%s",
                    (result["status_code"] if result else 0, task_id),
                )
                return {"task_id": task_id, "status": "failed"}

            execute(
                """UPDATE crawl_tasks SET status='done', http_status=%s,
                   finished_at=NOW(), raw_storage_uri=%s WHERE id=%s""",
                (result["status_code"], f"local://{task_id}", task_id),
            )
            return {"task_id": task_id, "status": "done", "url": result["final_url"]}

        except Exception as exc:
            execute(
                "UPDATE crawl_tasks SET status='failed', error_msg=%s, finished_at=NOW() WHERE id=%s",
                (str(exc)[:500], task_id),
            )
            log.error("Task %d failed: %s", task_id, exc)
            return {"task_id": task_id, "status": "failed", "error": str(exc)}

    def _respect_rate_limit(self, site_key: str, rps: float = 1.0) -> None:
        last = self._last_request_time.get(site_key, 0.0)
        wait = (1.0 / rps) - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    def _get_robots_parser(
        self, site_key: str, url: str
    ) -> urllib.robotparser.RobotFileParser | None:
        if site_key in self._robots_cache:
            return self._robots_cache[site_key]
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        rp = urllib.robotparser.RobotFileParser(robots_url)
        try:
            rp.read()
            self._robots_cache[site_key] = rp
            return rp
        except Exception:
            self._robots_cache[site_key] = None
            return None

    @staticmethod
    def _site_key_from_url(url: str) -> str:
        return urlparse(url).netloc
