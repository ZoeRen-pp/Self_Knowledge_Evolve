"""Background worker — crawler, pipeline, and stats as independent threads."""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
from pathlib import Path

# Ensure semcore is importable
_semcore_path = str(Path(__file__).parent / "semcore")
if _semcore_path not in sys.path:
    sys.path.insert(0, _semcore_path)

from semcore.core.context import PipelineContext

from src.app_factory import get_app
from src.config.settings import settings
from src.crawler.spider import Spider
from src.utils.health import startup_health_check
from src.utils.logging import setup_logging

log = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────────────

_IDLE_BACKOFF_START = 3
_IDLE_BACKOFF_MAX = 300
_MAX_RETRIES = 3
_RETRY_BACKOFF_MINUTES = [5, 30, 120]

_SEED_SOURCES: list[dict] = [
    # S-rank: Authoritative standards bodies
    {
        "site_key": "ietf-datatracker",
        "site_name": "IETF Datatracker",
        "home_url": "https://datatracker.ietf.org/",
        "source_rank": "S",
        "rate_limit_rps": 0.5,
        "seed_urls": [
            "https://datatracker.ietf.org/doc/html/rfc4271",
            "https://datatracker.ietf.org/doc/html/rfc4456",
            "https://datatracker.ietf.org/doc/html/rfc4760",
            "https://datatracker.ietf.org/doc/html/rfc7938",
            "https://datatracker.ietf.org/doc/html/rfc4364",
            "https://datatracker.ietf.org/doc/html/rfc4684",
            "https://datatracker.ietf.org/doc/html/rfc5065",
            "https://datatracker.ietf.org/doc/html/rfc6811",
            "https://datatracker.ietf.org/doc/html/rfc2328",
            "https://datatracker.ietf.org/doc/html/rfc5340",
            "https://datatracker.ietf.org/doc/html/rfc3630",
            "https://datatracker.ietf.org/doc/html/rfc5308",
            "https://datatracker.ietf.org/doc/html/rfc5305",
            "https://datatracker.ietf.org/doc/html/rfc3031",
            "https://datatracker.ietf.org/doc/html/rfc3032",
            "https://datatracker.ietf.org/doc/html/rfc3209",
            "https://datatracker.ietf.org/doc/html/rfc5036",
            "https://datatracker.ietf.org/doc/html/rfc8402",
            "https://datatracker.ietf.org/doc/html/rfc8986",
            "https://datatracker.ietf.org/doc/html/rfc9252",
            "https://datatracker.ietf.org/doc/html/rfc7432",
            "https://datatracker.ietf.org/doc/html/rfc7348",
            "https://datatracker.ietf.org/doc/html/rfc8365",
            "https://datatracker.ietf.org/doc/html/rfc9136",
            "https://datatracker.ietf.org/doc/html/rfc5765",
            "https://datatracker.ietf.org/doc/html/rfc7130",
            "https://datatracker.ietf.org/doc/html/rfc8668",
            "https://datatracker.ietf.org/doc/html/rfc791",
            "https://datatracker.ietf.org/doc/html/rfc8200",
            "https://datatracker.ietf.org/doc/html/rfc793",
            "https://datatracker.ietf.org/doc/html/rfc768",
            "https://datatracker.ietf.org/doc/html/rfc2131",
            "https://datatracker.ietf.org/doc/html/rfc1034",
            "https://datatracker.ietf.org/doc/html/rfc792",
            "https://datatracker.ietf.org/doc/html/rfc4443",
            "https://datatracker.ietf.org/doc/html/rfc3768",
            "https://datatracker.ietf.org/doc/html/rfc5798",
            "https://datatracker.ietf.org/doc/html/rfc5880",
            "https://datatracker.ietf.org/doc/html/rfc5881",
            "https://datatracker.ietf.org/doc/html/rfc2474",
            "https://datatracker.ietf.org/doc/html/rfc2475",
            "https://datatracker.ietf.org/doc/html/rfc2697",
            "https://datatracker.ietf.org/doc/html/rfc2698",
            "https://datatracker.ietf.org/doc/html/rfc2544",
            "https://datatracker.ietf.org/doc/html/rfc4303",
            "https://datatracker.ietf.org/doc/html/rfc7296",
            "https://datatracker.ietf.org/doc/html/rfc6241",
            "https://datatracker.ietf.org/doc/html/rfc8040",
            "https://datatracker.ietf.org/doc/html/rfc7950",
            "https://datatracker.ietf.org/doc/html/rfc8345",
            "https://datatracker.ietf.org/doc/html/rfc3022",
            "https://datatracker.ietf.org/doc/html/rfc6146",
            "https://datatracker.ietf.org/doc/html/rfc4601",
            "https://datatracker.ietf.org/doc/html/rfc3376",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    # A-rank: Major vendor documentation
    {
        "site_key": "huawei-info",
        "site_name": "Huawei Info Center",
        "home_url": "https://info.support.huawei.com/",
        "source_rank": "A",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://info.support.huawei.com/info-finder/encyclopedia/en/BGP.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/OSPF.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/MPLS.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/VXLAN.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/EVPN.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/SRv6.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/QoS.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/ACL.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/VLAN.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/NAT.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/DHCP.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/DNS.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/IPsec.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/BFD.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/VRRP.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/IS-IS.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/NETCONF.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/Segment+Routing.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/LACP.html",
            "https://info.support.huawei.com/info-finder/encyclopedia/en/STP.html",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    {
        "site_key": "juniper-techlib",
        "site_name": "Juniper TechLibrary",
        "home_url": "https://www.juniper.net/documentation/",
        "source_rank": "A",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://www.juniper.net/documentation/us/en/software/junos/bgp/topics/topic-map/bgp-overview.html",
            "https://www.juniper.net/documentation/us/en/software/junos/ospf/topics/topic-map/ospf-overview.html",
            "https://www.juniper.net/documentation/us/en/software/junos/mpls/topics/topic-map/mpls-overview.html",
            "https://www.juniper.net/documentation/us/en/software/junos/evpn-vxlan/topics/concept/evpn-vxlan-overview.html",
            "https://www.juniper.net/documentation/us/en/software/junos/is-is/topics/topic-map/is-is-overview.html",
            "https://www.juniper.net/documentation/us/en/software/junos/segment-routing/topics/concept/segment-routing-overview.html",
            "https://www.juniper.net/documentation/us/en/software/junos/high-availability/topics/topic-map/bfd.html",
            "https://www.juniper.net/documentation/us/en/software/junos/nat/topics/topic-map/nat-overview.html",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    {
        "site_key": "arista-docs",
        "site_name": "Arista Documentation",
        "home_url": "https://www.arista.com/en/um-eos/",
        "source_rank": "A",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://www.arista.com/en/um-eos/eos-border-gateway-protocol-bgp",
            "https://www.arista.com/en/um-eos/eos-open-shortest-path-first-version-3-ospfv3",
            "https://www.arista.com/en/um-eos/eos-vxlan",
            "https://www.arista.com/en/um-eos/eos-evpn-overview",
            "https://www.arista.com/en/um-eos/eos-multi-protocol-label-switching-mpls-overview",
            "https://www.arista.com/en/um-eos/eos-segment-routing",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    # B-rank: Technical learning / whitepapers
    {
        "site_key": "networklessons",
        "site_name": "NetworkLessons",
        "home_url": "https://networklessons.com/",
        "source_rank": "B",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-bgp",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-ospf",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-mpls",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-vxlan",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-qos",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-is-is",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-sd-wan",
            "https://networklessons.com/cisco/ccnp-encor-350-401/introduction-to-vrf-lite",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    {
        "site_key": "cloudflare-learn",
        "site_name": "Cloudflare Learning Center",
        "home_url": "https://www.cloudflare.com/learning/",
        "source_rank": "B",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://www.cloudflare.com/learning/network-layer/what-is-bgp/",
            "https://www.cloudflare.com/learning/network-layer/what-is-routing/",
            "https://www.cloudflare.com/learning/network-layer/what-is-a-router/",
            "https://www.cloudflare.com/learning/network-layer/what-is-an-autonomous-system/",
            "https://www.cloudflare.com/learning/network-layer/what-is-mpls/",
            "https://www.cloudflare.com/learning/security/glossary/what-is-bgp-hijacking/",
            "https://www.cloudflare.com/learning/ddos/glossary/open-systems-interconnection-model-osi/",
            "https://www.cloudflare.com/learning/network-layer/what-is-a-wan/",
            "https://www.cloudflare.com/learning/network-layer/what-is-a-lan/",
            "https://www.cloudflare.com/learning/network-layer/what-is-a-subnet/",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    {
        "site_key": "packetlife",
        "site_name": "PacketLife.net",
        "home_url": "https://packetlife.net/",
        "source_rank": "B",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://packetlife.net/blog/2008/sep/22/ospf-area-types/",
            "https://packetlife.net/blog/2009/jun/10/understanding-bgp-path-selection/",
            "https://packetlife.net/blog/2010/jan/19/mpls-fundamentals/",
            "https://packetlife.net/blog/2010/feb/1/vlan-trunking/",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
    {
        "site_key": "ipspace",
        "site_name": "ipSpace.net Blog",
        "home_url": "https://blog.ipspace.net/",
        "source_rank": "B",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://blog.ipspace.net/2024/01/bgp-labs-simple-routing-policy.html",
            "https://blog.ipspace.net/2022/09/evpn-bridging-routing.html",
            "https://blog.ipspace.net/2023/03/segment-routing-overview.html",
            "https://blog.ipspace.net/2022/03/vxlan-evpn-behind-curtain.html",
        ],
        "scope_rules": None,
        "extra_headers": None,
    },
]


# ── Helpers ──────────────────────────────────────────────────────────────────

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


# ── Thread: Crawler ──────────────────────────────────────────────────────────

def _crawler_thread(app, stop_event: threading.Event) -> None:
    """Continuously crawl pending tasks."""
    thread_name = "crawler"
    log.info("[%s] Thread started", thread_name)
    crawler_store = app.crawler_store or app.store
    spider = Spider(object_store=app.objects, store=crawler_store, knowledge_store=app.store)

    idle_count = 0
    try:
        while not stop_event.is_set():
            try:
                retried = _retry_failed_tasks(crawler_store)
                crawl_results = spider.run_pending_tasks(limit=settings.WORKER_CRAWL_LIMIT)
                has_work = len(crawl_results) > 0 or retried > 0

                if has_work:
                    idle_count = 0
                    log.info("[%s] Cycle: crawled=%d retried=%d",
                             thread_name, len(crawl_results), retried)
                else:
                    idle_count += 1
                    if idle_count <= _IDLE_BACKOFF_START:
                        log.debug("[%s] Idle cycle", thread_name)
            except Exception as exc:
                log.error("[%s] Error: %s", thread_name, exc, exc_info=True)
                idle_count = 0

            if idle_count > _IDLE_BACKOFF_START:
                backoff = min(
                    settings.WORKER_SLEEP_SECS * (2 ** (idle_count - _IDLE_BACKOFF_START)),
                    _IDLE_BACKOFF_MAX,
                )
                stop_event.wait(backoff)
            else:
                stop_event.wait(settings.WORKER_SLEEP_SECS)
    finally:
        spider.close()
        log.info("[%s] Thread stopped", thread_name)


# ── Thread: Pipeline ─────────────────────────────────────────────────────────

def _pipeline_thread(app, stop_event: threading.Event) -> None:
    """Continuously process raw documents through the pipeline."""
    thread_name = "pipeline"
    log.info("[%s] Thread started", thread_name)

    idle_count = 0
    try:
        while not stop_event.is_set():
            try:
                rows = app.store.fetchall(
                    """SELECT source_doc_id FROM documents
                       WHERE status = 'raw'
                       ORDER BY created_at ASC
                       LIMIT %s""",
                    (settings.WORKER_PIPELINE_LIMIT,),
                )
                doc_ids = [str(r["source_doc_id"]) for r in rows]

                if doc_ids:
                    idle_count = 0
                    for doc_id in doc_ids:
                        if stop_event.is_set():
                            break
                        ctx = PipelineContext(source_doc_id=doc_id)
                        try:
                            app.ingest_context(ctx)
                            log.info("[%s] Completed doc=%s errors=%d",
                                     thread_name, doc_id, len(ctx.errors))
                        except Exception as exc:
                            log.error("[%s] Failed doc=%s err=%s",
                                      thread_name, doc_id, exc, exc_info=True)
                else:
                    idle_count += 1
            except Exception as exc:
                log.error("[%s] Error: %s", thread_name, exc, exc_info=True)
                idle_count = 0

            # Pipeline checks less frequently — docs arrive via crawler
            sleep_secs = settings.WORKER_SLEEP_SECS * 2
            if idle_count > _IDLE_BACKOFF_START:
                backoff = min(
                    sleep_secs * (2 ** (idle_count - _IDLE_BACKOFF_START)),
                    _IDLE_BACKOFF_MAX,
                )
                stop_event.wait(backoff)
            else:
                stop_event.wait(sleep_secs)
    finally:
        log.info("[%s] Thread stopped", thread_name)


# ── Thread: Stats / Monitoring ───────────────────────────────────────────────

def _maintenance_thread(app, stop_event: threading.Event) -> None:
    """Periodic ontology maintenance: embedding dedup, LLM classification, cleanup."""
    thread_name = "maintenance"
    log.info("[%s] Thread started", thread_name)

    interval_hours = getattr(settings, "ONTOLOGY_MAINTENANCE_INTERVAL_HOURS", 24)
    interval_secs = interval_hours * 3600

    # Wait 5 minutes after startup before first run (let pipeline populate data first)
    stop_event.wait(300)

    try:
        while not stop_event.is_set():
            try:
                from src.governance.maintenance import OntologyMaintenance
                maint = OntologyMaintenance(
                    store=app.store, graph=app.graph, ontology=app.ontology,
                )
                stats = maint.run()
                log.info("[%s] Cycle complete: %s", thread_name, stats.get("final", {}))
            except Exception as exc:
                log.error("[%s] Error: %s", thread_name, exc, exc_info=True)

            stop_event.wait(interval_secs)
    finally:
        log.info("[%s] Thread stopped", thread_name)


def _stats_thread(app, stop_event: threading.Event) -> None:
    """Periodically collect system stats."""
    thread_name = "stats"
    log.info("[%s] Thread started", thread_name)

    try:
        from src.stats.collector import StatsCollector
        from src.stats.scheduler import StatsScheduler
        collector = StatsCollector(
            store=app.store, graph=app.graph, crawler_store=app.crawler_store,
        )
        scheduler = StatsScheduler(collector, store=app.store)
        scheduler.start()
        log.info("[%s] Stats scheduler running (5 min interval)", thread_name)

        # Block until stop event — scheduler has its own internal timer
        stop_event.wait()

        scheduler.stop()
    except Exception as exc:
        log.error("[%s] Failed to start: %s", thread_name, exc, exc_info=True)
    finally:
        log.info("[%s] Thread stopped", thread_name)


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> None:
    setup_logging(settings.LOG_LEVEL)
    if not startup_health_check():
        raise SystemExit("Startup health check failed.")

    app = get_app()
    crawler_store = app.crawler_store or app.store
    _auto_enqueue_seeds(crawler_store)

    log.info("Worker starting: 4 threads (crawler, pipeline, stats, maintenance)")

    stop_event = threading.Event()

    threads = [
        threading.Thread(target=_crawler_thread, args=(app, stop_event),
                         name="crawler", daemon=True),
        threading.Thread(target=_pipeline_thread, args=(app, stop_event),
                         name="pipeline", daemon=True),
        threading.Thread(target=_stats_thread, args=(app, stop_event),
                         name="stats", daemon=True),
        threading.Thread(target=_maintenance_thread, args=(app, stop_event),
                         name="maintenance", daemon=True),
    ]

    for t in threads:
        t.start()
        log.info("  Started thread: %s", t.name)

    log.info("Worker started: all 3 threads running")

    try:
        # Main thread waits for KeyboardInterrupt
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Shutdown requested...")
        stop_event.set()
        for t in threads:
            t.join(timeout=10)
        log.info("Worker stopped")


if __name__ == "__main__":
    main()
