"""
Simplified HTTP crawler with robots.txt compliance and rate limiting.
Not a full Scrapy spider — uses httpx for fetching, with curl_cffi
fallback for sites protected by TLS fingerprint detection (e.g. Cloudflare).
"""

from __future__ import annotations

import logging
import re
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
# NOTE: rfc-editor.org is NOT listed here because we fetch plain-text .txt URLs
# which do not trigger JS challenges.  HTML pages on rfc-editor.org were moved
# to plain-text via _normalize_url() before this set is consulted.
_TLS_FINGERPRINT_SITES: set[str] = set()

# Sites with broken / self-signed certificate chains → skip SSL verify
_SSL_SKIP_VERIFY_SITES = {"portal.3gpp.org"}

# Rewrite IETF Datatracker HTML wrapper URLs to RFC Editor plain-text URLs.
# The HTML wrapper at datatracker.ietf.org yields poorly structured extraction;
# the .txt version is machine-friendly and preserves section structure.
_DATATRACKER_RFC_RE = re.compile(
    r"https?://datatracker\.ietf\.org/doc/html/(rfc\d+)(?:[/?#].*)?$", re.I
)


class CrawlTask(TypedDict):
    id: int
    url: str
    site_key: str
    priority: int
    task_type: str


class Spider:
    def __init__(
        self,
        object_store: ObjectStore,
        store: RelationalStore,
        knowledge_store: RelationalStore | None = None,
    ) -> None:
        self._robots_cache: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._last_request_time: dict[str, float] = {}
        self._objects = object_store
        self._store = store                    # crawler DB (crawl_tasks, source_registry)
        self._knowledge_store = knowledge_store  # knowledge DB (documents)
        self._client = httpx.Client(
            timeout=30,
            follow_redirects=True,
            headers=DEFAULT_HEADERS,
            verify=_SSL_VERIFY,
        )

    # ── Public ────────────────────────────────────────────────────

    @staticmethod
    def normalize_url(url: str) -> str:
        """Rewrite known URL patterns to their canonical form for better content quality.

        Currently handles:
          datatracker.ietf.org/doc/html/rfcNNNN  →  www.rfc-editor.org/rfc/rfcNNNN.txt
        """
        m = _DATATRACKER_RFC_RE.match(url)
        if m:
            return f"https://www.rfc-editor.org/rfc/{m.group(1)}.txt"
        return url

    def fetch(self, url: str, extra_headers: dict | None = None) -> dict | None:
        """Fetch a URL. Returns {html, status_code, final_url, content_type} or None."""
        url = self.normalize_url(url)
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
        log.debug("Polling pending crawl tasks (limit=%d)", limit)
        tasks = self._store.fetchall(
            """
            SELECT ct.id, ct.url, ct.site_key, ct.priority, ct.task_type,
                   sr.rate_limit_rps, sr.extra_headers, sr.source_rank
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
        url = self.normalize_url(task["url"])  # rewrite datatracker → rfc-editor .txt etc.
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
                   finished_at=NOW(), raw_storage_uri=%s, content_hash=%s WHERE id=%s""",
                (result["status_code"], raw_uri, c_hash, task_id),
            )

            # Create document record in knowledge DB so pipeline can pick it up
            source_doc_id = self._create_document(task, raw_uri, c_hash, result)

            log.info(
                "Crawl task done: id=%s doc=%s http_status=%s raw_uri=%s bytes=%s",
                task_id,
                source_doc_id or "skipped",
                result["status_code"],
                raw_uri,
                len(html_bytes),
            )
            # Discover new links from this page
            discovered = self._discover_links(
                result["html"], result.get("final_url") or url, task["site_key"],
            )
            if discovered:
                log.info("Discovered %d new URLs from %s", discovered, url)

            return {
                "task_id": task_id, "status": "done",
                "url": result["final_url"], "raw_uri": raw_uri,
                "source_doc_id": source_doc_id,
                "links_discovered": discovered,
            }

        except Exception as exc:
            self._store.execute(
                "UPDATE crawl_tasks SET status='failed', error_msg=%s, finished_at=NOW() WHERE id=%s",
                (str(exc)[:500], task_id),
            )
            log.error("Task %d failed: %s", task_id, exc)
            return {"task_id": task_id, "status": "failed", "error": str(exc)}

    def _create_document(self, task: dict, raw_uri: str, c_hash: str, result: dict) -> str | None:
        """Create a document record in the knowledge DB for pipeline processing."""
        kstore = self._knowledge_store
        if kstore is None:
            return None
        import uuid
        source_doc_id = str(uuid.uuid4())
        try:
            kstore.execute(
                """INSERT INTO documents (
                    source_doc_id, crawl_task_id, site_key, source_url, canonical_url,
                    source_rank, crawl_time, content_hash,
                    raw_storage_uri, status
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, NOW(), %s,
                    %s, 'raw'
                )
                ON CONFLICT (source_doc_id) DO NOTHING""",
                (
                    source_doc_id, task["id"], task["site_key"],
                    task["url"], result.get("final_url") or task["url"],
                    task.get("source_rank") or "C", c_hash,
                    raw_uri,
                ),
            )
            return source_doc_id
        except Exception as exc:
            log.error("Failed to create document for task %s: %s", task["id"], exc)
            return None

    def _discover_links(self, html: str, base_url: str, site_key: str) -> int:
        """Extract same-site links from HTML and enqueue as new crawl tasks.

        Only follows links that:
        - Are on the same hostname as base_url
        - Match scope_rules (if defined) for this site_key
        - Haven't been crawled before (ON CONFLICT DO NOTHING)
        - Look like content pages (not images, css, js, anchors)
        """
        import re as _re
        from urllib.parse import urljoin, urlparse

        parsed_base = urlparse(base_url)
        base_host = parsed_base.netloc

        # Extract href values from <a> tags
        hrefs = _re.findall(r'<a[^>]+href=["\']([^"\'#]+)', html, _re.I)
        if not hrefs:
            return 0

        # Load scope rules for this site
        scope_row = self._store.fetchone(
            "SELECT scope_rules FROM source_registry WHERE site_key = %s",
            (site_key,),
        )
        scope_rules = scope_row.get("scope_rules") if scope_row else None
        allow_patterns = []
        deny_patterns = []
        if scope_rules and isinstance(scope_rules, dict):
            for pattern in scope_rules.get("allow", []):
                try:
                    allow_patterns.append(_re.compile(pattern))
                except _re.error:
                    pass
            for pattern in scope_rules.get("deny", []):
                try:
                    deny_patterns.append(_re.compile(pattern))
                except _re.error:
                    pass

        # Filter for non-content extensions
        _SKIP_EXT = {'.css', '.js', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico',
                     '.pdf', '.zip', '.tar', '.gz', '.mp4', '.mp3', '.woff', '.woff2', '.ttf'}

        enqueued = 0
        for href in hrefs:
            full_url = urljoin(base_url, href)
            parsed = urlparse(full_url)

            # Same host only
            if parsed.netloc != base_host:
                continue

            # Skip non-content
            path_lower = parsed.path.lower()
            if any(path_lower.endswith(ext) for ext in _SKIP_EXT):
                continue

            # Normalize: strip fragment, keep query
            clean_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
            if parsed.query:
                clean_url += f"?{parsed.query}"

            # Apply scope rules
            if allow_patterns and not any(p.search(clean_url) for p in allow_patterns):
                continue
            if deny_patterns and any(p.search(clean_url) for p in deny_patterns):
                continue

            # Enqueue (dedup via UNIQUE on url)
            try:
                self._store.execute(
                    """INSERT INTO crawl_tasks (site_key, url, task_type, priority, status, scheduled_at)
                       VALUES (%s, %s, 'discovered', 3, 'pending', NOW())
                       ON CONFLICT (url) DO NOTHING""",
                    (site_key, clean_url),
                )
                enqueued += 1
            except Exception:
                pass

        return enqueued

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

        # Fetch robots.txt with SSL tolerance (some sites have cert issues)
        try:
            resp = self._client.get(robots_url, follow_redirects=True)
            if resp.status_code == 200:
                rp = urllib.robotparser.RobotFileParser(robots_url)
                rp.parse(resp.text.splitlines())
                self._robots_cache[site_key] = rp
                return rp
            # Non-200 (403, 404, etc.) → assume no restrictions
            log.debug("robots.txt returned %d for %s, assuming allowed", resp.status_code, robots_url)
            self._robots_cache[site_key] = None
            return None
        except Exception as exc:
            log.debug("robots.txt fetch failed for %s: %s, assuming allowed", robots_url, exc)
            self._robots_cache[site_key] = None
            return None

    @staticmethod
    def _site_key_from_url(url: str) -> str:
        return urlparse(url).netloc
