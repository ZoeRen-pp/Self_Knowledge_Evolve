"""
Demo: fetch a webpage and run it through the full in-memory pipeline.
Shows exactly what gets stored in each system component.

Usage:
    python demo_pipeline.py [URL]

Default URL: https://en.wikipedia.org/wiki/Border_Gateway_Protocol
"""

from __future__ import annotations

import sys
import json
import uuid
import logging
import types
from pathlib import Path

# ── 0. Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.WARNING,          # suppress internal noise
    format="%(levelname)s  %(name)s  %(message)s",
)
log = logging.getLogger("demo")

# ── 1. Make semcore importable ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent / "semcore"))

# ── 2. Inject fake DB modules ───────────────────────────────────────────────
from src.dev import fake_postgres, fake_neo4j, fake_crawler_postgres

_db_mod = types.ModuleType("src.db")
_db_mod.postgres         = fake_postgres
_db_mod.neo4j_client     = fake_neo4j
_db_mod.crawler_postgres = fake_crawler_postgres
_db_mod.health_check     = lambda: {"postgres": True, "neo4j": True, "crawler_postgres": True}

sys.modules["src.db"]                  = _db_mod
sys.modules["src.db.postgres"]         = fake_postgres
sys.modules["src.db.neo4j_client"]     = fake_neo4j
sys.modules["src.db.crawler_postgres"] = fake_crawler_postgres

# ── 3. Fake ObjectStore (MinIO substitute) ──────────────────────────────────
from semcore.providers.base import ObjectStore

class MemoryObjectStore(ObjectStore):
    """In-memory object store — mimics MinIO put/get."""
    def __init__(self):
        self._store: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        uri = f"minio://{key}"
        self._store[uri] = data
        return uri

    def get(self, uri: str) -> bytes:
        key = uri
        if key in self._store:
            return self._store[key]
        # also try without minio:// prefix
        for k, v in self._store.items():
            if k == uri or k.endswith(uri.lstrip("/")):
                return v
        raise KeyError(f"Object not found: {uri}")

    def exists(self, uri: str) -> bool:
        return uri in self._store

    def keys(self):
        return list(self._store.keys())

    def size(self, uri: str) -> int:
        return len(self._store.get(uri, b""))

# ── 4. Patch fake_neo4j to capture writes ───────────────────────────────────
_neo4j_writes: list[dict] = []

_original_run_write = fake_neo4j.run_write

def _capturing_run_write(cypher: str, **params):
    _neo4j_writes.append({"cypher": cypher.strip(), "params": params})
    return []

fake_neo4j.run_write = _capturing_run_write

# Patch the session's execute_write too
class _CapturingTx:
    def run(self, cypher, **params):
        _capturing_run_write(cypher, **params)
        return fake_neo4j._FakeResult([])

class _CapturingSession(fake_neo4j._FakeSession):
    def execute_write(self, fn):
        return fn(_CapturingTx())

import contextlib

@contextlib.contextmanager
def _capturing_get_session():
    yield _CapturingSession()

fake_neo4j.get_session = _capturing_get_session

# ── 5. Seed ontology ─────────────────────────────────────────────────────────
from src.dev.seed import seed_from_registry
seed_from_registry()

# ── 6. Build app with fake providers ─────────────────────────────────────────
from semcore.app import AppConfig, SemanticApp
from semcore.operators.base import LoggingMiddleware, TimingMiddleware
from src.config.settings import settings
from src.providers.postgres_store         import PostgresRelationalStore
from src.providers.neo4j_store            import Neo4jGraphStore
from src.providers.crawler_postgres_store import CrawlerPostgresRelationalStore
from src.ontology.registry                import OntologyRegistry
from src.ontology.yaml_provider           import YAMLOntologyProvider
from src.governance.confidence_scorer     import TelecomConfidenceScorer
from src.governance.conflict_detector     import TelecomConflictDetector
from src.governance.evolution_gate        import TelecomEvolutionGate
from src.pipeline.pipeline_factory        import build_pipeline
from src.operators                        import ALL_OPERATORS

# LLM provider: use real Gemini if LLM_ENABLED=true, else Noop
from semcore.providers.base import LLMProvider, EmbeddingProvider

class NoopLLM(LLMProvider):
    def complete(self, prompt, **kwargs): return ""
    def extract_structured(self, prompt, schema, **kwargs): return {}
    def is_enabled(self): return False
    def ping(self): return True

class NoopEmbedding(EmbeddingProvider):
    def encode(self, texts, **kwargs): return []
    def dimension(self): return 1024
    def ping(self): return True

if settings.LLM_ENABLED:
    from src.providers.anthropic_llm import ClaudeLLMProvider
    llm_provider = ClaudeLLMProvider()
    print(f"[LLM] Gemini enabled — model={settings.LLM_MODEL}")
else:
    llm_provider = NoopLLM()
    print("[LLM] disabled (set LLM_ENABLED=true in .env to activate)")

object_store = MemoryObjectStore()
registry     = OntologyRegistry.from_default()

config = AppConfig(
    llm               = llm_provider,
    embedding         = NoopEmbedding(),
    graph             = Neo4jGraphStore(),
    store             = PostgresRelationalStore(),
    objects           = object_store,
    ontology          = YAMLOntologyProvider(registry),
    confidence_scorer = TelecomConfidenceScorer(),
    conflict_detector = TelecomConflictDetector(),
    evolution_gate    = TelecomEvolutionGate(),
    crawler_store     = CrawlerPostgresRelationalStore(),
    operators         = ALL_OPERATORS,
    middlewares       = [TimingMiddleware()],
)
config.pipeline = build_pipeline()
app = SemanticApp(config)

# ── 7. Source-rank inference (mirrors source_registry authority tiers) ────────
def _infer_source_rank(url: str) -> str:
    """Map URL hostname to source authority rank.

    Tiers mirror the confidence formula's source_authority weights:
      S (1.00) — IETF / 3GPP / ITU-T / IEEE standards bodies
      A (0.85) — Major network equipment vendors
      B (0.65) — Whitepapers, educational content (default)
      C (0.40) — Blogs, forums, community content

    Extend by appending hostnames to the appropriate suffix list.
    """
    from urllib.parse import urlparse
    hostname = urlparse(url).hostname or ""
    S_SUFFIXES = ("rfc-editor.org", "datatracker.ietf.org", "ietf.org",
                  "3gpp.org", "ieee.org", "itu.int")
    A_SUFFIXES = ("cisco.com", "huawei.com", "juniper.net", "nokia.com",
                  "zte.com.cn", "arista.com", "paloaltonetworks.com", "h3c.com")
    C_SUFFIXES = ("reddit.com", "stackoverflow.com", "zhihu.com", "csdn.net",
                  "medium.com", "blogspot.com")
    for s in S_SUFFIXES:
        if hostname == s or hostname.endswith("." + s):
            return "S"
    for s in A_SUFFIXES:
        if hostname == s or hostname.endswith("." + s):
            return "A"
    for s in C_SUFFIXES:
        if hostname == s or hostname.endswith("." + s):
            return "C"
    return "B"

_RANK_LABELS = {"S": "IETF/3GPP/IEEE standard", "A": "vendor documentation",
                "B": "whitepaper/educational", "C": "blog/forum"}

# ── 8. Fetch webpage ──────────────────────────────────────────────────────────
import httpx

URL = sys.argv[1] if len(sys.argv) > 1 else "_builtin_"

# Built-in BGP configuration guide sample (telecom domain, mix of conceptual /
# procedural / constraint knowledge — representative of what the pipeline sees)
_BUILTIN_TEXT = """# BGP Configuration Guide

Border Gateway Protocol (BGP) is the routing protocol that manages how packets
are routed across the internet between autonomous systems (AS). BGP is classified
as a path-vector protocol and makes routing decisions based on paths, network
policies, and rule-sets configured by network administrators.

## Prerequisites

Before configuring BGP, the following must be completed:
1. IP addresses must be assigned to all interfaces.
2. Static routes or IGP (OSPF/IS-IS) must be configured for loopback reachability.
3. The router must have a unique AS number assigned by IANA or your service provider.

## Basic BGP Configuration Steps

Step 1: Enable BGP and define the local AS number.

  system-view
  bgp 65001

Step 2: Configure BGP peer relationships (neighbors).

  peer 10.0.0.2 as-number 65002
  peer 10.0.0.2 description UPSTREAM-ISP

  For IBGP sessions within the same AS, use loopback addresses:
  peer 192.168.1.1 connect-interface LoopBack0

Step 3: Advertise networks into BGP.

  network 203.0.113.0 255.255.255.0
  network 198.51.100.0 255.255.255.128

Step 4: Apply routing policies (route-map / route-policy).

  peer 10.0.0.2 route-policy EXPORT-TO-ISP export
  peer 10.0.0.2 route-policy IMPORT-FROM-ISP import

## BGP Attributes and Path Selection

BGP selects the best path using attributes evaluated in this order:
- Highest LOCAL_PREF (preferred within the AS)
- Lowest AS_PATH length (fewer hops preferred for EBGP)
- Origin code: IGP < EGP < Incomplete
- Lowest MED (Multi-Exit Discriminator) from the same neighboring AS
- EBGP paths preferred over IBGP paths
- Lowest IGP cost to the next-hop
- Lowest BGP router-ID as a tiebreaker

## Constraints and Limitations

BGP is only applicable when connecting to external autonomous systems (EBGP)
or when scaling internal routing across large networks (IBGP with route
reflectors). For small-scale networks with a single exit point, static routes
or OSPF are more appropriate.

The maximum number of BGP peers on a single device is hardware-dependent;
consult your vendor's data sheet. Exceeding this limit causes session drops.

BGP sessions require TCP port 179 to be open between peers. Firewall rules
blocking this port will prevent session establishment.

## Route Reflector Configuration

In large IBGP deployments, a full mesh of IBGP sessions is impractical.
Route reflectors (RR) solve this by allowing an RR to re-advertise IBGP routes
to its clients without requiring a full mesh.

  bgp 65001
   group RR-CLIENTS internal
   peer RR-CLIENTS reflect-client

Clients do not need special configuration; they simply peer with the RR as
a normal IBGP neighbor.

## Troubleshooting BGP

If BGP sessions do not establish, check the following:
1. Verify TCP connectivity on port 179: telnet <peer-ip> 179
2. Confirm AS numbers match the configuration on both sides.
3. Check that authentication MD5 keys are identical if configured.
4. Ensure TTL is sufficient for EBGP multi-hop sessions (default TTL=1).

Common BGP state transitions:
- Idle → Active: BGP is initiating a TCP connection.
- Active → OpenSent: TCP connection established, BGP OPEN message sent.
- OpenSent → OpenConfirm: OPEN received, awaiting KEEPALIVE.
- OpenConfirm → Established: Session fully operational.
"""

print(f"\n{'='*70}")
print("  TELECOM KB — PIPELINE DEMO")
if URL == "_builtin_":
    print(f"  Source: built-in BGP configuration guide")
else:
    print(f"  URL: {URL}")
print(f"{'='*70}\n")

import hashlib

if URL == "_builtin_":
    print("[ SOURCE ] Using built-in BGP config guide text")
    html_bytes = _BUILTIN_TEXT.encode("utf-8")
    final_url  = "local://bgp-config-guide.txt"
    print(f"  bytes = {len(html_bytes):,}")
else:
    print(f"[ FETCH ] Fetching via Spider (curl_cffi + browser UA)...")
    from src.crawler.spider import Spider as _Spider
    _spider = _Spider(object_store=object_store, store=None, knowledge_store=None)
    result = _spider.fetch(URL)
    if not result or result["status_code"] >= 400:
        print(f"  FAILED: status={result['status_code'] if result else 'none'}")
        sys.exit(1)
    html_bytes = result["html"].encode("utf-8", errors="replace")
    final_url  = result["final_url"]
    print(f"  status={result['status_code']}  bytes={len(html_bytes):,}  type={result['content_type'][:50]}")

# ── 8. Store raw content in fake MinIO ───────────────────────────────────────
c_hash   = hashlib.sha256(html_bytes).hexdigest()
raw_key  = f"raw/{c_hash}.txt"
raw_uri  = object_store.put(raw_key, html_bytes, content_type="text/plain")
final_url = URL if URL != "_builtin_" else "local://bgp-config-guide.txt"

print(f"\n[ MINIO/raw ] Stored raw HTML")
print(f"  uri   = {raw_uri}")
print(f"  sha256= {c_hash}")
print(f"  size  = {len(html_bytes):,} bytes")

# ── 9. Create document record in fake PG ─────────────────────────────────────
source_doc_id = str(uuid.uuid4())
source_rank   = _infer_source_rank(final_url) if URL != "_builtin_" else "B"
fake_postgres.execute(
    """INSERT INTO documents (
        source_doc_id, site_key, source_url, canonical_url,
        source_rank, content_hash, raw_storage_uri, status
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'raw')""",
    (source_doc_id, "local", final_url, final_url,
     source_rank, c_hash, raw_uri),
)
print(f"\n[ PG/documents ] Created document record")
print(f"  source_doc_id = {source_doc_id}")
print(f"  source_rank   = {source_rank}  ({_RANK_LABELS.get(source_rank, '')})")
print(f"  status        = raw")

# ── 10. Run pipeline ──────────────────────────────────────────────────────────
print(f"\n[ PIPELINE ] Running 6-stage pipeline...")
print(f"  Stage 1: Ingest (extract + clean + quality gate)")
print(f"  Stage 2: Segment (EDU segmentation + RST relations)")
print(f"  Stage 3: Align (ontology mapping)")
print(f"  Stage 3b: Evolve (candidate scoring)")
print(f"  Stage 4: Extract (SPO triples)")
print(f"  Stage 5: Dedup (SimHash + fact merge)")
print(f"  Stage 6: Index (Neo4j + PG facts)")

try:
    ctx = app.ingest(source_doc_id)
    errors = getattr(ctx, "errors", []) or []
    if errors:
        print(f"\n  [!] Pipeline errors: {errors}")
except Exception as exc:
    import traceback
    print(f"\n  [!] Pipeline exception: {exc}")
    traceback.print_exc()

# ── 11. Dump results ──────────────────────────────────────────────────────────

pg = fake_postgres  # shorthand

def q(sql, *params):
    return pg.fetchall(sql, params if params else ())

SEP   = "-" * 70
THICK = "=" * 70

# ── documents ──
print(f"\n{THICK}")
print("  PG: documents")
print(THICK)
docs = q("SELECT source_doc_id, title, doc_type, language, source_rank, status, "
         "content_hash, raw_storage_uri, cleaned_storage_uri FROM documents")
for d in docs:
    for k, v in d.items():
        if v is not None:
            print(f"  {k:<28} {v}")

# ── MinIO buckets ──
print(f"\n{THICK}")
print("  MinIO (object store)")
print(THICK)
for uri in object_store.keys():
    size = object_store.size(uri)
    print(f"  {uri}")
    print(f"    size = {size:,} bytes")
    if uri.startswith("minio://cleaned/"):
        preview = object_store.get(uri).decode("utf-8", errors="replace")[:400].replace("\n", " ")
        print(f"    preview = {preview!r}")

# ── segments ──
print(f"\n{THICK}")
print("  PG: segments  (EDUs)")
print(THICK)
segs = q("SELECT segment_id, segment_index, segment_type, section_title, "
         "token_count, confidence, raw_text FROM segments ORDER BY segment_index")
print(f"  total = {len(segs)}")
for s in segs[:20]:   # cap at 20
    print(f"\n  [{s['segment_index']}] type={s['segment_type']}  tokens={s['token_count']}  conf={s['confidence']:.2f}")
    if s.get("section_title"):
        print(f"       section = {s['section_title']}")
    raw = (s.get("raw_text") or "").replace("\n", " ")[:200].encode("ascii", "replace").decode("ascii")
    print(f"       text    = {raw!r}")
if len(segs) > 20:
    print(f"\n  ... and {len(segs)-20} more segments")

# ── RST relations ──
print(f"\n{THICK}")
print("  PG: t_rst_relation  (inter-EDU discourse relations)")
print(THICK)
rsts = q("SELECT relation_type, src_edu_id, dst_edu_id, relation_source FROM t_rst_relation")
print(f"  total = {len(rsts)}")
from collections import Counter
rst_counts = Counter(r["relation_type"] for r in rsts)
for rel_type, cnt in rst_counts.most_common():
    print(f"  {rel_type:<30} {cnt}")

# ── segment_tags ──
print(f"\n{THICK}")
print("  PG: segment_tags  (ontology node mappings)")
print(THICK)
tags = q("SELECT tag_type, ontology_node_id, COUNT(*) as cnt FROM segment_tags "
         "GROUP BY tag_type, ontology_node_id ORDER BY cnt DESC")
print(f"  total tag rows = {sum(t['cnt'] for t in tags)}")
print(f"  unique ontology nodes hit = {len(set(t['ontology_node_id'] for t in tags if t['ontology_node_id']))}")
for t in tags[:20]:
    nid = t['ontology_node_id'] or "(none)"
    print(f"  {t['tag_type']:<18} {nid:<35} x{t['cnt']}")
if len(tags) > 20:
    print(f"  ... and {len(tags)-20} more")

# ── facts ──
print(f"\n{THICK}")
print("  PG: facts  (SPO triples)")
print(THICK)
facts = q("SELECT fact_id, subject, predicate, object, confidence, lifecycle_state FROM facts ORDER BY confidence DESC")
print(f"  total = {len(facts)}")
for f in facts[:30]:
    print(f"  ({f['subject']})  --[{f['predicate']}]-->  ({f['object']})  conf={f['confidence']:.2f}  state={f['lifecycle_state']}")
if len(facts) > 30:
    print(f"  ... and {len(facts)-30} more")

# ── evidence ──
print(f"\n{THICK}")
print("  PG: evidence")
print(THICK)
evs = q("SELECT evidence_id, fact_id, source_rank, extraction_method, evidence_score, exact_span FROM evidence ORDER BY evidence_score DESC")
print(f"  total = {len(evs)}")
for e in evs[:15]:
    span = (e.get("exact_span") or "")[:120].replace("\n", " ")
    print(f"  score={e['evidence_score']:.2f}  method={e['extraction_method']}  rank={e['source_rank']}")
    if span:
        print(f"    span = {span!r}")
if len(evs) > 15:
    print(f"  ... and {len(evs)-15} more")

# ── evolution candidates ──
print(f"\n{THICK}")
print("  PG: evolution_candidates  (newly discovered terms)")
print(THICK)
cands = q("SELECT normalized_form, source_count, review_status, composite_score FROM evolution_candidates ORDER BY composite_score DESC")
print(f"  total = {len(cands)}")
for c in cands[:20]:
    print(f"  {c['normalized_form']:<40} score={c['composite_score']:.2f}  status={c['review_status']}")
if len(cands) > 20:
    print(f"  ... and {len(cands)-20} more")

# ── Neo4j writes ──
print(f"\n{THICK}")
print("  Neo4j: captured write operations")
print(THICK)
print(f"  total write calls = {len(_neo4j_writes)}")
node_creates = [w for w in _neo4j_writes if "MERGE" in w["cypher"]]
by_label: dict[str, list] = {}
for w in node_creates:
    cypher = w["cypher"]
    for label in ["SourceDocument", "KnowledgeSegment", "Fact", "Evidence", "OntologyNode",
                  "MechanismNode", "MethodNode", "ConditionRuleNode", "ScenarioPatternNode"]:
        if label in cypher:
            by_label.setdefault(label, []).append(w)
            break

for label, writes in by_label.items():
    print(f"\n  :{label}  ({len(writes)} nodes merged)")
    sample = writes[0]
    params = sample["params"]
    for k, v in list(params.items())[:6]:
        vstr = str(v)[:80]
        print(f"    {k:<28} {vstr}")

# ── Summary ──
print(f"\n{THICK}")
print("  SUMMARY")
print(THICK)
seg_count   = len(q("SELECT segment_id FROM segments"))
fact_count  = len(q("SELECT fact_id FROM facts"))
ev_count    = len(q("SELECT evidence_id FROM evidence"))
tag_count   = len(q("SELECT tag_id FROM segment_tags"))
cand_count  = len(q("SELECT candidate_id FROM evolution_candidates"))
rst_count   = len(q("SELECT nn_relation_id FROM t_rst_relation"))
minio_keys  = object_store.keys()

print(f"  MinIO objects        : {len(minio_keys)}  ({', '.join(k.split('/')[0].replace('minio://','') for k in minio_keys[:4])})")
print(f"  PG segments (EDUs)   : {seg_count}")
print(f"  PG RST relations     : {rst_count}")
print(f"  PG segment_tags      : {tag_count}")
print(f"  PG facts (SPO)       : {fact_count}")
print(f"  PG evidence          : {ev_count}")
print(f"  PG evolution cands   : {cand_count}")
print(f"  Neo4j write ops      : {len(_neo4j_writes)}")
print()