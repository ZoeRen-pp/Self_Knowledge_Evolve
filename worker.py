"""Background worker — crawler, pipeline, and stats as independent threads."""

from __future__ import annotations

import datetime
import json
import logging
import sys
import threading
import time
from pathlib import Path

# Use Windows system certificate store so Python's OpenSSL trusts the same CAs
# as the OS (needed for api.deepseek.com and other services not in certifi).
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

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
    # Plain-text RFC Editor URLs — avoid the HTML wrapper on datatracker.ietf.org
    # which yields poorly structured extraction.  The spider's normalize_url()
    # rewrites any remaining datatracker URLs that arrive via link discovery.
    {
        "site_key": "rfc-editor",
        "site_name": "RFC Editor",
        "home_url": "https://www.rfc-editor.org/",
        "source_rank": "S",
        "rate_limit_rps": 0.5,
        "seed_urls": [
            "https://www.rfc-editor.org/rfc/rfc4271.txt",   # BGP-4
            "https://www.rfc-editor.org/rfc/rfc4456.txt",   # BGP Route Reflectors
            "https://www.rfc-editor.org/rfc/rfc4760.txt",   # Multiprotocol BGP
            "https://www.rfc-editor.org/rfc/rfc7938.txt",   # BGP in DC
            "https://www.rfc-editor.org/rfc/rfc4364.txt",   # BGP/MPLS IP VPNs
            "https://www.rfc-editor.org/rfc/rfc4684.txt",   # Constrained Route Distribution
            "https://www.rfc-editor.org/rfc/rfc5065.txt",   # Confederations
            "https://www.rfc-editor.org/rfc/rfc6811.txt",   # BGP Prefix Origin Validation
            "https://www.rfc-editor.org/rfc/rfc2328.txt",   # OSPFv2
            "https://www.rfc-editor.org/rfc/rfc5340.txt",   # OSPFv3
            "https://www.rfc-editor.org/rfc/rfc3630.txt",   # OSPF-TE
            "https://www.rfc-editor.org/rfc/rfc5308.txt",   # IS-IS for IPv6
            "https://www.rfc-editor.org/rfc/rfc5305.txt",   # IS-IS TE
            "https://www.rfc-editor.org/rfc/rfc3031.txt",   # MPLS Architecture
            "https://www.rfc-editor.org/rfc/rfc3032.txt",   # MPLS Label Stack
            "https://www.rfc-editor.org/rfc/rfc3209.txt",   # RSVP-TE
            "https://www.rfc-editor.org/rfc/rfc5036.txt",   # LDP
            "https://www.rfc-editor.org/rfc/rfc8402.txt",   # Segment Routing Architecture
            "https://www.rfc-editor.org/rfc/rfc8986.txt",   # SRv6 Network Programming
            "https://www.rfc-editor.org/rfc/rfc9252.txt",   # BGP Overlay Services (EVPN)
            "https://www.rfc-editor.org/rfc/rfc7432.txt",   # BGP MPLS-Based EVPN
            "https://www.rfc-editor.org/rfc/rfc7348.txt",   # VXLAN
            "https://www.rfc-editor.org/rfc/rfc8365.txt",   # NVO3 using EVPN
            "https://www.rfc-editor.org/rfc/rfc9136.txt",   # IP Prefix Advertisement in EVPN
            "https://www.rfc-editor.org/rfc/rfc791.txt",    # IPv4
            "https://www.rfc-editor.org/rfc/rfc8200.txt",   # IPv6
            "https://www.rfc-editor.org/rfc/rfc793.txt",    # TCP
            "https://www.rfc-editor.org/rfc/rfc768.txt",    # UDP
            "https://www.rfc-editor.org/rfc/rfc2131.txt",   # DHCP
            "https://www.rfc-editor.org/rfc/rfc1034.txt",   # DNS Concepts
            "https://www.rfc-editor.org/rfc/rfc792.txt",    # ICMP
            "https://www.rfc-editor.org/rfc/rfc4443.txt",   # ICMPv6
            "https://www.rfc-editor.org/rfc/rfc5798.txt",   # VRRPv3
            "https://www.rfc-editor.org/rfc/rfc5880.txt",   # BFD
            "https://www.rfc-editor.org/rfc/rfc5881.txt",   # BFD for IPv4/IPv6
            "https://www.rfc-editor.org/rfc/rfc2474.txt",   # DiffServ Field
            "https://www.rfc-editor.org/rfc/rfc2475.txt",   # DiffServ Architecture
            "https://www.rfc-editor.org/rfc/rfc2697.txt",   # srTCM
            "https://www.rfc-editor.org/rfc/rfc2698.txt",   # trTCM
            "https://www.rfc-editor.org/rfc/rfc4303.txt",   # ESP
            "https://www.rfc-editor.org/rfc/rfc7296.txt",   # IKEv2
            "https://www.rfc-editor.org/rfc/rfc6241.txt",   # NETCONF
            "https://www.rfc-editor.org/rfc/rfc8040.txt",   # RESTCONF
            "https://www.rfc-editor.org/rfc/rfc7950.txt",   # YANG 1.1
            "https://www.rfc-editor.org/rfc/rfc3022.txt",   # NAT
            "https://www.rfc-editor.org/rfc/rfc4601.txt",   # PIM-SM
            "https://www.rfc-editor.org/rfc/rfc3376.txt",   # IGMPv3
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
    # A-rank: Open-source reference implementations (primary authoritative source
    # for configuration syntax, CLI options, and protocol behaviour on Linux)
    {
        "site_key": "frr-docs",
        "site_name": "FRRouting Documentation",
        "home_url": "https://docs.frrouting.org/en/latest/",
        "source_rank": "A",
        "rate_limit_rps": 0.5,
        "seed_urls": [
            "https://docs.frrouting.org/en/latest/bgp.html",
            "https://docs.frrouting.org/en/latest/ospfd.html",
            "https://docs.frrouting.org/en/latest/isisd.html",
            "https://docs.frrouting.org/en/latest/bfd.html",
            "https://docs.frrouting.org/en/latest/vrrpd.html",
            "https://docs.frrouting.org/en/latest/ldpd.html",
            "https://docs.frrouting.org/en/latest/mpls.html",
            "https://docs.frrouting.org/en/latest/sr.html",
            "https://docs.frrouting.org/en/latest/evpn.html",
            "https://docs.frrouting.org/en/latest/pbrd.html",
            "https://docs.frrouting.org/en/latest/vrf.html",
            "https://docs.frrouting.org/en/latest/pim.html",
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
    {
        "site_key": "noction",
        "site_name": "Noction Blog",
        "home_url": "https://www.noction.com/blog/",
        "source_rank": "B",
        "rate_limit_rps": 0.3,
        "seed_urls": [
            "https://www.noction.com/blog/bgp-best-path-selection-process",
            "https://www.noction.com/blog/bgp-route-reflector",
            "https://www.noction.com/blog/bgp-confederations",
            "https://www.noction.com/blog/mpls-architecture",
            "https://www.noction.com/blog/segment-routing",
            "https://www.noction.com/blog/vxlan-overview",
            "https://www.noction.com/blog/ospf-protocol",
            "https://www.noction.com/blog/bgp-communities",
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
    """Continuously process raw documents through the pipeline.

    Uses a long-lived thread pool with continuous feed: each worker grabs a
    new document as soon as it finishes the previous one. A slow document on
    one worker never blocks the others.

    LLM availability is a hard requirement: if LLM cannot be reached the
    thread pauses and retries every 2 minutes until it recovers.
    """
    thread_name = "pipeline"
    max_workers = settings.WORKER_PIPELINE_WORKERS
    log.info("[%s] Thread started (workers=%d)", thread_name, max_workers)

    from concurrent.futures import ThreadPoolExecutor, Future, FIRST_COMPLETED, wait
    from src.utils.llm_extract import LLMExtractor

    _LLM_RETRY_SECS = 120
    _POLL_SECS = settings.WORKER_SLEEP_SECS * 2

    def _process_one(doc_id: str) -> tuple[str, int, str | None]:
        ctx = PipelineContext(source_doc_id=doc_id)
        try:
            app.ingest_context(ctx)
            return (doc_id, len(ctx.errors), None)
        except Exception as exc:
            return (doc_id, -1, str(exc))

    def _fetch_raw_ids(limit: int) -> list[str]:
        rows = app.store.fetchall(
            """SELECT source_doc_id FROM documents
               WHERE status = 'raw'
               ORDER BY created_at ASC
               LIMIT %s""",
            (limit,),
        )
        return [str(r["source_doc_id"]) for r in rows]

    def _handle_done(fut: Future) -> None:
        doc_id, err_count, err_msg = fut.result()
        if err_msg:
            log.error("[%s] Failed doc=%s err=%s", thread_name, doc_id, err_msg)
        else:
            log.info("[%s] Completed doc=%s errors=%d",
                     thread_name, doc_id, err_count)

    idle_count = 0
    pool = ThreadPoolExecutor(max_workers=max_workers,
                              thread_name_prefix="pipeline-worker")
    in_flight: set[Future] = set()

    try:
        while not stop_event.is_set():

            # ── Hard LLM check ───────────────────────────────────────────
            if not LLMExtractor().ping(timeout=10.0):
                log.warning(
                    "[%s] LLM not available — pipeline paused. "
                    "Retrying in %ds.", thread_name, _LLM_RETRY_SECS,
                )
                stop_event.wait(_LLM_RETRY_SECS)
                continue

            # ── Harvest completed futures ─────────────────────────────────
            done = {f for f in in_flight if f.done()}
            for f in done:
                try:
                    _handle_done(f)
                except Exception as exc:
                    log.error("[%s] Result error: %s", thread_name, exc)
            in_flight -= done

            # ── Fill pool to capacity ─────────────────────────────────────
            free_slots = max_workers - len(in_flight)
            if free_slots > 0:
                try:
                    in_flight_ids = set()
                    doc_ids = _fetch_raw_ids(free_slots)
                    if doc_ids:
                        idle_count = 0
                        for did in doc_ids:
                            if stop_event.is_set():
                                break
                            if did not in in_flight_ids:
                                fut = pool.submit(_process_one, did)
                                in_flight.add(fut)
                                in_flight_ids.add(did)
                    elif not in_flight:
                        idle_count += 1
                except Exception as exc:
                    log.error("[%s] Error: %s", thread_name, exc, exc_info=True)

            # ── Wait for next event ───────────────────────────────────────
            if in_flight:
                wait(in_flight, timeout=_POLL_SECS,
                     return_when=FIRST_COMPLETED)
            else:
                if idle_count > _IDLE_BACKOFF_START:
                    backoff = min(
                        _POLL_SECS * (2 ** (idle_count - _IDLE_BACKOFF_START)),
                        _IDLE_BACKOFF_MAX,
                    )
                    stop_event.wait(backoff)
                else:
                    stop_event.wait(_POLL_SECS)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        log.info("[%s] Thread stopped", thread_name)


# ── Thread: Stats / Monitoring ───────────────────────────────────────────────

# Maintenance targets 03:00 CST but runs at the first poll after that time
# each CST day. This survives host sleep/suspend: if the machine is asleep
# at 03:00 and wakes at 07:00, the cycle runs at 07:00. At most one run per
# CST date, persisted to disk so worker restarts don't trigger a re-run.
_MAINT_TARGET_HOUR = 3            # 03:00 CST
_MAINT_POLL_SECS = 300            # 5-minute polling interval
_MAINT_STATE_PATH = Path(__file__).parent / "logs" / "maintenance_state.json"
_CST = datetime.timezone(datetime.timedelta(hours=8))


def _load_maint_state() -> dict:
    try:
        return json.loads(_MAINT_STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_maint_state(state: dict) -> None:
    try:
        _MAINT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _MAINT_STATE_PATH.write_text(json.dumps(state), encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to persist maintenance state: %s", exc)


def _maintenance_thread(app, stop_event: threading.Event) -> None:
    """Ontology maintenance: one run per CST day, first poll after 03:00.

    Uses a short polling interval and catch-up semantics so that a sleeping
    or suspended host doesn't miss the daily window. Last successful run
    date is persisted to disk; worker restarts don't re-run the same day.
    """
    thread_name = "maintenance"
    log.info("[%s] Thread started", thread_name)

    state = _load_maint_state()
    last_run_date = state.get("last_run_date")
    if last_run_date:
        log.info("[%s] Loaded state: last_run_date=%s", thread_name, last_run_date)
    log.info(
        "[%s] Polling every %ds, target hour %02d:00 CST",
        thread_name, _MAINT_POLL_SECS, _MAINT_TARGET_HOUR,
    )

    # When an attempt fails, back off from the normal 5-min poll to 1h
    # so a persistent failure doesn't spam the log.
    failure_retry_at: datetime.datetime | None = None

    try:
        while not stop_event.is_set():
            now = datetime.datetime.now(tz=_CST)
            today_target = now.replace(
                hour=_MAINT_TARGET_HOUR, minute=0, second=0, microsecond=0,
            )
            today_str = now.date().isoformat()
            past_backoff = failure_retry_at is None or now >= failure_retry_at
            due = (
                now >= today_target
                and last_run_date != today_str
                and past_backoff
            )

            if due:
                delay_min = (now - today_target).total_seconds() / 60
                log.info(
                    "[%s] Starting maintenance cycle for %s "
                    "(target 03:00 CST, actual %s, delay %.0f min)",
                    thread_name, today_str, now.strftime("%H:%M"), delay_min,
                )
                succeeded = False
                for attempt in range(1, 4):
                    try:
                        from src.governance.maintenance import OntologyMaintenance
                        maint = OntologyMaintenance(
                            store=app.store, graph=app.graph, ontology=app.ontology,
                        )
                        stats = maint.run()
                        log.info(
                            "[%s] Cycle complete: %s",
                            thread_name, stats.get("final", {}),
                        )
                        succeeded = True
                        break
                    except Exception as exc:
                        log.error(
                            "[%s] Attempt %d/3 failed: %s",
                            thread_name, attempt, exc, exc_info=True,
                        )
                        if attempt < 3 and stop_event.wait(60):
                            break

                if succeeded:
                    last_run_date = today_str
                    failure_retry_at = None
                    _save_maint_state({"last_run_date": last_run_date})
                else:
                    failure_retry_at = now + datetime.timedelta(hours=1)
                    log.warning(
                        "[%s] All 3 attempts failed; next retry at %s",
                        thread_name, failure_retry_at.strftime("%H:%M"),
                    )

            if stop_event.wait(_MAINT_POLL_SECS):
                break
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

    # LLM is required for pipeline — fail fast if not reachable
    from src.utils.llm_extract import LLMExtractor
    log.info("Checking LLM connectivity...")
    if not LLMExtractor().ping():
        raise SystemExit(
            "LLM is not available. "
            "Fix LLM_API_KEY / LLM_BASE_URL in .env and restart."
        )
    log.info("LLM connectivity: ok")

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

    log.info("Worker started: all 4 threads running")

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
