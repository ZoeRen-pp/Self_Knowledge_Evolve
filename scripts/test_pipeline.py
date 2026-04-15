"""
全流程 Pipeline 效果测试 (Stage 1-5) — 多 URL 爬取，内存模式，无需外部服务。

用法：
    python scripts/test_pipeline.py

每个 URL 在 tmp/ 下生成文件：
    <label>_stage{1..5}.txt

质量校验：
    - facts 必须通过 doc_id JOIN 隔离，不得跨文档污染
    - LLM 熔断在每个文档前重置，确保独立性
    - segment_tags 在 segments 删除前清理（正确顺序）
    - 每文档打印 fact 质量报告：ontology 命中率、跨域检测
"""

from __future__ import annotations

import hashlib
import re
import sys
import types
import uuid
import logging
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "semcore"))

# ── 日志 ──────────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s  %(message)s")
for _name in (
    "src.pipeline.stages.stage1_ingest",
    "src.pipeline.stages.stage2_segment",
    "src.pipeline.stages.stage3_align",
    "src.pipeline.stages.stage4_extract",
    "src.pipeline.stages.stage5_dedup",
    "src.utils.llm_extract",
    "src.pipeline.preprocessing.extractor",
):
    logging.getLogger(_name).setLevel(logging.INFO)

# ── fake DB ───────────────────────────────────────────────────────────────────
from src.dev import fake_postgres, fake_neo4j, fake_crawler_postgres

_db_mod = types.ModuleType("src.db")
_db_mod.postgres         = fake_postgres
_db_mod.neo4j_client     = fake_neo4j
_db_mod.crawler_postgres = fake_crawler_postgres
_db_mod.health_check     = lambda: {}
sys.modules["src.db"]                   = _db_mod
sys.modules["src.db.postgres"]          = fake_postgres
sys.modules["src.db.neo4j_client"]      = fake_neo4j
sys.modules["src.db.crawler_postgres"]  = fake_crawler_postgres

# ── 本体 ──────────────────────────────────────────────────────────────────────
from src.ontology.registry import OntologyRegistry
from src.dev.seed import seed_from_registry
registry = OntologyRegistry.from_default()
seed_from_registry()

# 预建 ontology node ID 集合（用于 fact 质量校验）
# registry.nodes is a dict keyed by node id; values have 'id' field.
ALL_ONTOLOGY_NODES: set[str] = set(registry.nodes.keys())
print(f"  [ontology] {len(ALL_ONTOLOGY_NODES)} nodes loaded for fact quality check")

# ── 内存对象存储 ───────────────────────────────────────────────────────────────
from semcore.providers.base import ObjectStore

class MemObjectStore(ObjectStore):
    def __init__(self):
        self._data: dict[str, bytes] = {}
    def put(self, key, data, *, content_type="application/octet-stream"):
        uri = f"minio://{key}"
        self._data[uri] = data
        return uri
    def get(self, uri):
        return self._data[uri]
    def exists(self, uri):
        return uri in self._data

objects = MemObjectStore()

# ── LLM & App ─────────────────────────────────────────────────────────────────
from src.utils.llm_extract import LLMExtractor
llm = LLMExtractor()

# Pre-flight LLM reachability check — run in a daemon thread so Windows DNS
# hangs don't block the whole test. If the thread doesn't finish within the
# deadline, we treat LLM as unreachable and disable it for this run.
if llm.is_enabled():
    import queue as _queue, threading as _threading

    _result: _queue.Queue[bool] = _queue.Queue()

    def _ping() -> None:
        try:
            _result.put(llm.ping(timeout=5.0))
        except Exception:
            _result.put(False)

    _t = _threading.Thread(target=_ping, daemon=True)
    _t.start()
    _t.join(timeout=7.0)
    _reachable = False
    try:
        _reachable = _result.get_nowait()
    except _queue.Empty:
        pass  # thread still blocked on DNS → LLM unreachable

    if not _reachable:
        llm._enabled = False
        print("  ⚠ LLM pre-flight ping failed — running with LLM=DISABLED")


class FakeApp:
    store         = fake_postgres
    crawler_store = fake_crawler_postgres
    objects       = objects
    ontology      = registry
    llm           = llm

app = FakeApp()

# Register DB keep-alive so that when the LLM circuit breaker blocks waiting for
# LLM recovery, SQLite connections don't time out (real PG would need the same).
def _db_keep_alive() -> None:
    try:
        fake_postgres.ping()
    except Exception:
        pass

llm.register_keep_alive(_db_keep_alive)

# ── 待测 URL 列表 ──────────────────────────────────────────────────────────────
# (source_rank, label, url)
TEST_URLS = [
    # S — IETF 标准文档
    ("S", "rfc_bgp",
     "https://datatracker.ietf.org/doc/html/rfc4271"),

    # S — 3GPP 5G 架构概览
    ("S", "3gpp_5g_arch",
     "https://www.3gpp.org/technologies/5g-system-overview"),

    # A — Cisco IOS BGP 配置指南
    ("A", "cisco_bgp",
     "https://www.cisco.com/c/en/us/td/docs/ios-xml/ios/iproute_bgp/configuration/xe-16/irg-xe-16-book/configuring-a-basic-bgp-network.html"),

    # A — Huawei OSPF 配置指南
    ("A", "huawei_ospf",
     "https://support.huawei.com/enterprise/en/doc/EDOC1100278509/12de9b3d/configuring-ospf-basic-functions"),

    # A — Juniper BGP 概览
    ("A", "juniper_bgp",
     "https://www.juniper.net/documentation/us/en/software/junos/bgp/topics/topic-map/bgp-overview.html"),

    # B — Nokia 5G Transport whitepaper
    ("B", "nokia_5g_transport",
     "https://www.nokia.com/networks/mobile-networks/5g/transport/"),

    # C — NetworkLessons BGP 教程（博客）
    ("C", "blog_bgp_basics",
     "https://networklessons.com/bgp/bgp-attributes-and-path-selection"),
]

# ── 辅助 ──────────────────────────────────────────────────────────────────────
SEP  = "=" * 72
SEP2 = "─" * 72

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

(ROOT / "tmp").mkdir(exist_ok=True)

from src.dev.fake_postgres import fetchone, execute, fetchall
from src.pipeline.stages.stage1_ingest import IngestStage
from src.pipeline.stages.stage2_segment import SegmentStage
from src.pipeline.stages.stage3_align   import AlignStage
from src.pipeline.stages.stage4_extract import ExtractStage
from src.pipeline.stages.stage5_dedup   import DedupStage
from semcore.core.context import PipelineContext

import httpx

stage1 = IngestStage()
stage2 = SegmentStage()
stage3 = AlignStage()
stage4 = ExtractStage()
stage5 = DedupStage()


def _fact_quality(facts: list[dict]) -> dict:
    """Analyse fact quality: ontology hit rate and cross-domain detection."""
    if not facts:
        return {"total": 0, "both_valid": 0, "hit_rate": 0.0, "subject_prefixes": {}, "object_prefixes": {}}

    both_valid = 0
    subj_prefixes: Counter = Counter()
    obj_prefixes: Counter  = Counter()

    for f in facts:
        subj, obj = f.get("subject", ""), f.get("object", "")
        s_prefix = subj.split(".")[0] if "." in subj else subj[:8]
        o_prefix = obj.split(".")[0] if "." in obj else obj[:8]
        subj_prefixes[s_prefix] += 1
        obj_prefixes[o_prefix]  += 1
        if subj in ALL_ONTOLOGY_NODES and obj in ALL_ONTOLOGY_NODES:
            both_valid += 1

    return {
        "total": len(facts),
        "both_valid": both_valid,
        "hit_rate": both_valid / len(facts),
        "subject_prefixes": dict(subj_prefixes.most_common(5)),
        "object_prefixes":  dict(obj_prefixes.most_common(5)),
    }


def _cleanup_doc(doc_id: str) -> None:
    """Isolate documents: remove all DB state for this doc_id.

    Order matters:
      1. segment_tags (FK→ segments) must be deleted BEFORE segments
      2. evidence (FK→ facts) must be deleted BEFORE facts
      3. facts then segments then documents last
    """
    # 1. segment_tags → must come before segments are deleted
    execute(
        "DELETE FROM segment_tags WHERE segment_id IN "
        "(SELECT segment_id FROM segments WHERE source_doc_id = ?)",
        (doc_id,),
    )
    # 2. RST relations → same dependency on segments
    execute(
        "DELETE FROM t_rst_relation WHERE src_edu_id IN "
        "(SELECT segment_id FROM segments WHERE source_doc_id = ?)",
        (doc_id,),
    )
    # 3. segments
    execute("DELETE FROM segments WHERE source_doc_id = ?", (doc_id,))
    # 4. evidence → must come before facts
    execute("DELETE FROM evidence WHERE source_doc_id = ?", (doc_id,))
    # 5. facts (those whose only evidence was from this doc)
    #    Any fact with no remaining evidence is orphaned; delete it.
    execute(
        "DELETE FROM facts WHERE fact_id NOT IN (SELECT DISTINCT fact_id FROM evidence)",
        (),
    )
    # 6. evolution_candidates are global but small; clear all for a clean next run
    execute("DELETE FROM evolution_candidates", ())
    # 7. documents row
    execute("DELETE FROM documents WHERE source_doc_id = ?", (doc_id,))


# ── 总览汇总行 ────────────────────────────────────────────────────────────────
summary_rows: list[dict] = []

print(f"\n{SEP}")
print(f"  Pipeline 多源爬取测试  LLM={'enabled' if llm.is_enabled() else 'DISABLED'}")
print(f"  URLs: {len(TEST_URLS)}")
print(SEP)

for rank, label, url in TEST_URLS:
    safe = re.sub(r"[^\w]", "_", label)[:40]
    print(f"\n{SEP2}")
    print(f"  [{rank}] {label}")
    print(f"  {url}")

    # ── 每文档前重置 LLM 熔断器 ───────────────────────────────────────────────
    # 确保一个文档的临时网络故障不影响后续文档的 LLM 可用性
    llm.reset_circuit_breaker()

    # ── 抓取 ──────────────────────────────────────────────────────────────────
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True)
        raw_bytes = resp.content
        http_status = resp.status_code
        final_url = str(resp.url)
    except Exception as exc:
        print(f"  [FETCH ERROR] {exc}")
        summary_rows.append({"label": label, "rank": rank, "error": str(exc)})
        continue

    if http_status >= 400:
        print(f"  [SKIP] HTTP {http_status}")
        summary_rows.append({"label": label, "rank": rank, "error": f"HTTP {http_status}"})
        continue

    print(f"  HTTP {http_status}  {len(raw_bytes)} bytes  final={final_url[:80]}")

    # ── Stage 1 ───────────────────────────────────────────────────────────────
    c_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw_uri = objects.put(f"raw/{c_hash}.html", raw_bytes)
    doc_id = str(uuid.uuid4())

    execute(
        """INSERT INTO documents
               (source_doc_id, site_key, source_url, canonical_url,
                source_rank, raw_storage_uri, status, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, 'raw', ?)""",
        (doc_id, label, url, final_url, rank, raw_uri, c_hash),
    )

    ctx = PipelineContext(source_doc_id=doc_id)
    ctx = stage1.process(ctx, app)

    doc = fetchone("SELECT * FROM documents WHERE source_doc_id = ?", (doc_id,))
    status = doc.get("status", "?")

    cleaned_uri = doc.get("cleaned_storage_uri") or ""
    cleaned_body = ""
    if cleaned_uri:
        try:
            cleaned_body = objects.get(cleaned_uri).decode("utf-8", "replace")
        except Exception:
            cleaned_body = "(读取失败)"

    print(f"  Stage1: status={status}  doc_type={doc.get('doc_type')}  "
          f"title={doc.get('title', '')[:50]!r}  cleaned={len(cleaned_body)}chars")

    s1_lines = [
        f"[{rank}] {label}",
        f"url           : {url}",
        f"final_url     : {final_url}",
        f"status        : {status}",
        f"doc_type      : {doc.get('doc_type')}",
        f"title         : {doc.get('title')}",
        f"language      : {doc.get('language')}",
        f"raw_bytes     : {len(raw_bytes)}",
        f"cleaned_bytes : {len(cleaned_body)}",
        "",
        "── cleaned text ──",
        cleaned_body,
    ]
    (ROOT / "tmp" / f"{safe}_stage1.txt").write_text("\n".join(s1_lines), encoding="utf-8")

    if status not in ("cleaned",):
        print(f"  [SKIP] stage1 quality gate failed")
        _cleanup_doc(doc_id)
        summary_rows.append({"label": label, "rank": rank, "error": f"stage1 {status}"})
        continue

    # ── Stage 2 ───────────────────────────────────────────────────────────────
    ctx = stage2.process(ctx, app)

    segs = fetchall(
        "SELECT * FROM segments WHERE source_doc_id = ? ORDER BY segment_index",
        (doc_id,),
    )
    seg_ids_sql = ",".join(f"'{s['segment_id']}'" for s in segs)
    rst_relations = fetchall(
        f"SELECT * FROM t_rst_relation WHERE src_edu_id IN ({seg_ids_sql}) ORDER BY src_edu_id",
        (),
    ) if seg_ids_sql else []

    type_dist = Counter(s["segment_type"] for s in segs)
    total_tokens = sum(s.get("token_count", 0) for s in segs)
    seg_idx_map = {str(s["segment_id"]): s.get("segment_index", "?") for s in segs}

    print(f"  Stage2: segs={len(segs)}  tokens={total_tokens}  rst={len(rst_relations)}  "
          f"types={dict(type_dist)}")

    s2_lines = [
        f"[{rank}] {label}",
        f"分段数: {len(segs)}  总tokens: {total_tokens}  RST关系: {len(rst_relations)}",
        f"类型分布: {dict(type_dist)}",
        "",
    ]
    for seg in segs:
        path_str = " > ".join(seg.get("section_path") or []) if seg.get("section_path") else ""
        s2_lines.append(SEP2)
        s2_lines.append(
            f"[{seg.get('segment_index', 0):03d}] type={seg['segment_type']:<18} "
            f"tok={seg.get('token_count', 0):<5} conf={seg.get('confidence', 0):.2f}"
        )
        if path_str:
            s2_lines.append(f"     path : {path_str}")
        if seg.get("section_title"):
            s2_lines.append(f"     title: {seg['section_title']}")
        for line in (seg.get("raw_text") or "").strip().splitlines():
            s2_lines.append(f"    {line}")
        s2_lines.append("")

    if rst_relations:
        s2_lines += ["", "── RST 关系 ──"]
        for r in rst_relations:
            src, dst = str(r["src_edu_id"]), str(r["dst_edu_id"])
            s2_lines.append(
                f"  [{seg_idx_map.get(src, src[:8])}] ──{r['relation_type']}"
                f"/{r.get('nuclearity','NN')}──> [{seg_idx_map.get(dst, dst[:8])}]"
            )
    (ROOT / "tmp" / f"{safe}_stage2.txt").write_text("\n".join(s2_lines), encoding="utf-8")

    # ── Stage 3 ───────────────────────────────────────────────────────────────
    ctx = stage3.process(ctx, app)

    all_tags = fetchall(
        """SELECT st.*, s.segment_index, s.segment_type, s.raw_text, s.token_count
           FROM segment_tags st
           JOIN segments s ON st.segment_id = s.segment_id
           WHERE s.source_doc_id = ?
           ORDER BY s.segment_index, st.tag_type""",
        (doc_id,),
    )
    candidates = fetchall(
        "SELECT normalized_form, candidate_type, source_count, review_status "
        "FROM evolution_candidates ORDER BY source_count DESC LIMIT 30",
        (),
    )
    tag_type_dist = Counter(t["tag_type"] for t in all_tags)
    segs_with_canonical = len({t["segment_id"] for t in all_tags if t["tag_type"] == "canonical"})

    # Isolation check: detect any tags from OTHER documents leaking in
    all_db_tags_count = fetchone("SELECT COUNT(*) as n FROM segment_tags", ())
    this_doc_tags_count = len(all_tags)
    leaked_tags = (all_db_tags_count.get("n", 0) if all_db_tags_count else 0) - this_doc_tags_count

    print(f"  Stage3: tags={len(all_tags)}  dist={dict(tag_type_dist)}  "
          f"canonical_segs={segs_with_canonical}/{len(segs)}  candidates={len(candidates)}"
          + (f"  ⚠ leaked_tags={leaked_tags}" if leaked_tags > 0 else ""))

    s3_lines = [
        f"[{rank}] {label}",
        f"标签总数: {len(all_tags)}  类型: {dict(tag_type_dist)}",
        f"canonical覆盖: {segs_with_canonical}/{len(segs)} 段",
        f"候选词: {len(candidates)}",
        f"隔离检查: DB中segment_tags总数={all_db_tags_count.get('n', '?') if all_db_tags_count else '?'}, "
        f"本文档={this_doc_tags_count}, 泄漏={leaked_tags}",
        "",
    ]
    tags_by_seg: dict = defaultdict(list)
    for t in all_tags:
        tags_by_seg[str(t["segment_id"])].append(t)

    for seg in segs:
        sid = str(seg["segment_id"])
        seg_tags = tags_by_seg.get(sid, [])
        s3_lines.append(SEP2)
        s3_lines.append(
            f"[{seg.get('segment_index', 0):03d}] type={seg['segment_type']:<18} "
            f"tok={seg.get('token_count', 0)}  {'(no tags)' if not seg_tags else ''}"
        )
        for line in (seg.get("raw_text") or "").strip().splitlines():
            s3_lines.append(f"    {line}")
        if seg_tags:
            s3_lines.append("  → tags:")
            for t in seg_tags:
                node_str = f" node={t.get('ontology_node_id') or '-'}"
                s3_lines.append(
                    f"     [{t['tag_type']:<16}] {t.get('tag_value',''):<35} "
                    f"conf={t.get('confidence', 0):.2f}  tagger={t.get('tagger','?')}{node_str}"
                )
        s3_lines.append("")

    if candidates:
        s3_lines += ["", "── 候选词（evolution_candidates）──"]
        for c in candidates:
            s3_lines.append(
                f"  {c['normalized_form']:<40} type={c.get('candidate_type','?'):<12} "
                f"n={c.get('source_count', 0)}"
            )
    (ROOT / "tmp" / f"{safe}_stage3.txt").write_text("\n".join(s3_lines), encoding="utf-8")

    # ── Stage 4 ───────────────────────────────────────────────────────────────
    ctx = stage4.process(ctx, app)

    # 通过 evidence JOIN 过滤，严格隔离本文档 facts，不受 DELETE ALL 时序影响
    facts = fetchall(
        """SELECT DISTINCT f.* FROM facts f
           JOIN evidence e ON f.fact_id = e.fact_id
           WHERE e.source_doc_id = ?
           ORDER BY f.confidence DESC""",
        (doc_id,),
    )
    evidence = fetchall(
        "SELECT * FROM evidence WHERE source_doc_id = ?", (doc_id,)
    )
    ev_by_fact: dict = defaultdict(list)
    for e in evidence:
        ev_by_fact[e["fact_id"]].append(e)
    ev_method = {e["fact_id"]: e.get("extraction_method", "?") for e in evidence}
    method_dist = Counter(ev_method.get(f["fact_id"], "?") for f in facts)

    quality = _fact_quality(facts)
    hit_pct = f"{quality['hit_rate']*100:.0f}%"

    print(f"  Stage4: facts={len(facts)}  methods={dict(method_dist)}  evidence={len(evidence)}")
    print(f"          ontology_hit={quality['both_valid']}/{quality['total']} ({hit_pct})  "
          f"subj_pfx={quality['subject_prefixes']}  obj_pfx={quality['object_prefixes']}")

    s4_lines = [
        f"[{rank}] {label}",
        f"三元组: {len(facts)}  方法: {dict(method_dist)}  evidence: {len(evidence)}",
        f"Ontology命中率: {quality['both_valid']}/{quality['total']} ({hit_pct})",
        f"Subject前缀分布: {quality['subject_prefixes']}",
        f"Object前缀分布:  {quality['object_prefixes']}",
        "",
        "── Facts ──",
    ]
    for f in facts:
        method = ev_method.get(f["fact_id"], "?")
        subj_ok = "✓" if f.get("subject") in ALL_ONTOLOGY_NODES else "✗"
        obj_ok  = "✓" if f.get("object")  in ALL_ONTOLOGY_NODES else "✗"
        s4_lines.append(
            f"  [{method:<12}] conf={f.get('confidence', 0):.3f}  "
            f"[{subj_ok}]{f['subject']}  ──[{f['predicate']}]──>  [{obj_ok}]{f['object']}"
        )
        evs = ev_by_fact.get(f["fact_id"], [])
        for e in evs:
            s4_lines.append(
                f"              ← seg={str(e.get('segment_id',''))[:12]}  "
                f"rank={e.get('source_rank','?')}  score={e.get('evidence_score', 0):.3f}"
            )
    (ROOT / "tmp" / f"{safe}_stage4.txt").write_text("\n".join(s4_lines), encoding="utf-8")

    # ── Stage 5 ───────────────────────────────────────────────────────────────
    ctx = stage5.process(ctx, app)

    # 读 Stage 5 结果
    active_facts = fetchall(
        """SELECT DISTINCT f.* FROM facts f
           JOIN evidence e ON f.fact_id = e.fact_id
           WHERE e.source_doc_id = ? AND f.lifecycle_state = 'active'
           ORDER BY f.confidence DESC""",
        (doc_id,),
    )
    merged_facts = fetchall(
        """SELECT DISTINCT f.* FROM facts f
           JOIN evidence e ON f.fact_id = e.fact_id
           WHERE e.source_doc_id = ? AND f.lifecycle_state = 'superseded'""",
        (doc_id,),
    )
    conflict_facts = fetchall(
        """SELECT DISTINCT f.* FROM facts f
           JOIN evidence e ON f.fact_id = e.fact_id
           WHERE e.source_doc_id = ? AND f.lifecycle_state = 'conflicted'""",
        (doc_id,),
    )

    print(f"  Stage5: active={len(active_facts)}  merged={len(merged_facts)}  "
          f"conflicted={len(conflict_facts)}")

    s5_lines = [
        f"[{rank}] {label}",
        f"Stage5 Dedup 结果:",
        f"  active facts   : {len(active_facts)}",
        f"  merged (super) : {len(merged_facts)}",
        f"  conflicted     : {len(conflict_facts)}",
        "",
        "── Active Facts after Dedup ──",
    ]
    for f in active_facts:
        method = ev_method.get(f["fact_id"], "?")
        s5_lines.append(
            f"  [{method:<12}] conf={f.get('confidence', 0):.3f}  "
            f"{f['subject']}  ──[{f['predicate']}]──>  {f['object']}"
        )
    if conflict_facts:
        s5_lines += ["", "── Conflicted Facts ──"]
        for f in conflict_facts:
            s5_lines.append(
                f"  conf={f.get('confidence', 0):.3f}  "
                f"{f['subject']}  ──[{f['predicate']}]──>  {f['object']}"
            )
    (ROOT / "tmp" / f"{safe}_stage5.txt").write_text("\n".join(s5_lines), encoding="utf-8")

    summary_rows.append({
        "label": label, "rank": rank,
        "segs": len(segs), "tokens": total_tokens,
        "rst": len(rst_relations), "types": dict(type_dist),
        "tags": len(all_tags), "candidates": len(candidates),
        "facts": len(facts), "methods": dict(method_dist),
        "active_after_dedup": len(active_facts),
        "merged": len(merged_facts),
        "conflicted": len(conflict_facts),
        "hit_rate": quality["hit_rate"],
        "leaked_tags": leaked_tags,
    })

    # ── 文档间隔离清理（正确顺序）──────────────────────────────────────────────
    _cleanup_doc(doc_id)

# ══════════════════════════════════════════════════════════════════════════════
# 汇总表
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  汇总")
print(SEP)
print(f"  {'label':<25} {'rank'} {'segs':>5} {'tok':>6} {'rst':>4} "
      f"{'tags':>5} {'cand':>5} {'facts':>6} {'active':>7} {'hit%':>5} {'leak':>5}")
print(f"  {'-'*25} {'-'*4} {'-'*5} {'-'*6} {'-'*4} "
      f"{'-'*5} {'-'*5} {'-'*6} {'-'*7} {'-'*5} {'-'*5}")
for r in summary_rows:
    if "error" in r:
        print(f"  {r['label']:<25} [{r['rank']}]  ERROR: {r['error']}")
    else:
        leak_str = f"⚠{r['leaked_tags']}" if r.get("leaked_tags", 0) > 0 else "ok"
        print(
            f"  {r['label']:<25} [{r['rank']}]"
            f"  {r['segs']:>4}  {r['tokens']:>5}  {r['rst']:>3}"
            f"  {r['tags']:>4}  {r['candidates']:>4}  {r['facts']:>5}"
            f"  {r['active_after_dedup']:>6}  {r['hit_rate']*100:>4.0f}%  {leak_str:>5}"
        )

print(f"\n  输出目录: {ROOT / 'tmp'}/")
print(f"  文件命名: <label>_stage{{1..5}}.txt")
print(SEP)

# ── 最终 DB 状态检查（验证清理彻底）─────────────────────────────────────────
print("\n  [DB 最终状态检查]")
for tbl in ("documents", "segments", "segment_tags", "facts", "evidence", "evolution_candidates"):
    row = fetchone(f"SELECT COUNT(*) as n FROM {tbl}", ())
    n = row.get("n", "?") if row else "?"
    status_str = "✓ clean" if n == 0 else f"⚠ {n} rows remain"
    print(f"    {tbl:<25} {status_str}")
print()
