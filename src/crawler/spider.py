"""
Simplified HTTP crawler with robots.txt compliance and rate limiting.
Not a full Scrapy spider — uses httpx for fetching, with curl_cffi
fallback for sites protected by TLS fingerprint detection (e.g. Cloudflare).
"""

from __future__ import annotations

import logging
import time
import urllib.robotparser
from typing import TypedDict
from urllib.parse import urlparse

import httpx

try:
    from curl_cffi import requests as _cffi
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

try:
    import certifi
    _SSL_VERIFY = certifi.where()
except ImportError:
    _SSL_VERIFY = True

from semcore.providers.base import ObjectStore, RelationalStore

log = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

DEFAULT_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,zh;q=0.8",
}

# Sites known to use Cloudflare / TLS fingerprint detection → use curl_cffi
_TLS_FINGERPRINT_SITES = {"www.rfc-editor.org"}

# Sites with broken / self-signed certificate chains → skip SSL verify
_SSL_SKIP_VERIFY_SITES = {"portal.3gpp.org"}


class CrawlTask(TypedDict):
    id: int
    url: str
    site_key: str
    priority: int
    task_type: str


class Spider:
    def __init__(self, object_store: ObjectStore, store: RelationalStore) -> None:
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request_time: dict[str, float] = {}
        self._objects = object_store
        self._store = store
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
            verify=_SSL_VERIFY,
        )

    # ── Public ────────────────────────────────────────────────────

    def fetch(self, url: str, extra_headers: dict | None = None) -> dict | None:
        """Fetch a URL. Returns {html, status_code, final_url, content_type} or None."""
        site_key = self._site_key_from_url(url)
        self._respect_rate_limit(site_key)
        hostname = urlparse(url).hostname or ""

        # Route: TLS fingerprint protected sites → curl_cffi
        if hostname in _TLS_FINGERPRINT_SITES:
            return self._fetch_cffi(url, extra_headers)

        # Route: broken SSL sites → httpx with verify=False
        if hostname in _SSL_SKIP_VERIFY_SITES:
            return self._fetch_no_verify(url, extra_headers)

        try:
            headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
            resp = self._client.get(url, headers=headers)
            self._last_request_time[site_key] = time.monotonic()

            # If httpx gets 403, try curl_cffi fallback (might be fingerprint block)
            if resp.status_code == 403 and _HAS_CURL_CFFI:
                log.info("httpx got 403 for %s, retrying with curl_cffi", url)
                return self._fetch_cffi(url, extra_headers)

            return {
                "html":         resp.text,
                "status_code":  resp.status_code,
                "final_url":    str(resp.url),
                "content_type": resp.headers.get("content-type", ""),
            }
        except httpx.ConnectError as exc:
            # SSL errors on connect — retry with verify=False if not already
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) and verify is not False:
                log.info("SSL verify failed for %s, retrying without verification", url)
                return self._fetch_no_verify(url, extra_headers)
            log.warning("Fetch failed for %s: %s", url, exc)
            return None
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
        tasks = self._store.fetchall(
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

    # ── Fetch helpers ──────────────────────────────────────────────

    def _fetch_cffi(self, url: str, extra_headers: dict | None = None) -> dict | None:
        """Fetch using curl_cffi with Chrome TLS fingerprint impersonation."""
        if not _HAS_CURL_CFFI:
            log.warning("curl_cffi not installed; cannot bypass TLS fingerprint for %s", url)
            return None
        site_key = self._site_key_from_url(url)
        try:
            headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
            resp = _cffi.get(url, headers=headers, impersonate="chrome", timeout=30)
            self._last_request_time[site_key] = time.monotonic()
            return {
                "html":         resp.text,
                "status_code":  resp.status_code,
                "final_url":    str(resp.url),
                "content_type": resp.headers.get("content-type", ""),
            }
        except Exception as exc:
            log.warning("curl_cffi fetch failed for %s: %s", url, exc)
            return None

    def _fetch_no_verify(self, url: str, extra_headers: dict | None = None) -> dict | None:
        """Fetch with SSL verification disabled (for sites with broken cert chains)."""
        site_key = self._site_key_from_url(url)
        try:
            headers = {**DEFAULT_HEADERS, **(extra_headers or {})}
            with httpx.Client(timeout=30, follow_redirects=True, verify=False) as client:
                resp = client.get(url, headers=headers)
            self._last_request_time[site_key] = time.monotonic()
            return {
                "html":         resp.text,
                "status_code":  resp.status_code,
                "final_url":    str(resp.url),
                "content_type": resp.headers.get("content-type", ""),
            }
        except Exception as exc:
            log.warning("Fetch (no-verify) failed for %s: %s", url, exc)
            return None

    # ── Private ───────────────────────────────────────────────────

    def _process_task(self, task: dict) -> dict:
        task_id = task["id"]
        url = task["url"]
        log.info("Crawl task start: id=%s url=%s", task_id, url)

        # Mark running
        self._store.execute(
            "UPDATE crawl_tasks SET status='running', started_at=NOW() WHERE id=%s",
            (task_id,),
        )

        try:
            # Check robots
            if not self.check_robots(task["site_key"], url):
                self._store.execute(
                    "UPDATE crawl_tasks SET status='skipped', finished_at=NOW() WHERE id=%s",
                    (task_id,),
                )
                return {"task_id": task_id, "status": "skipped", "reason": "robots_disallowed"}

            result = self.fetch(url, extra_headers=task.get("extra_headers") or {})

            if result is None or result["status_code"] >= 400:
                self._store.execute(
                    "UPDATE crawl_tasks SET status='failed', http_status=%s, finished_at=NOW() WHERE id=%s",
                    (result["status_code"] if result else 0, task_id),
                )
                log.warning("Crawl task failed: id=%s status=%s", task_id, result["status_code"] if result else 0)
                return {"task_id": task_id, "status": "failed"}

            html_bytes = result["html"].encode("utf-8", errors="replace")
            # Use content hash as key so identical content is never duplicated
            # and different content (e.g. retries with updated page) is never overwritten
            import hashlib
            c_hash = hashlib.sha256(html_bytes).hexdigest()
            raw_key = f"raw/{c_hash}.html"
            raw_uri = self._objects.put(
                raw_key,
                html_bytes,
                content_type=result.get("content_type") or "text/html",
            )

            self._store.execute(
                """UPDATE crawl_tasks SET status='done', http_status=%s,
                   finished_at=NOW(), raw_storage_uri=%s WHERE id=%s""",
                (result["status_code"], raw_uri, task_id),
            )
            log.info(
                "Crawl task done: id=%s http_status=%s raw_uri=%s bytes=%s",
                task_id,
                result["status_code"],
                raw_uri,
                len(html_bytes),
            )
            return {"task_id": task_id, "status": "done", "url": result["final_url"], "raw_uri": raw_uri}

        except Exception as exc:
            self._store.execute(
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
