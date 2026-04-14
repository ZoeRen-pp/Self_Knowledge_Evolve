"""
全流程 Pipeline 效果测试 (Stage 1-4) — 多 URL 爬取，内存模式，无需外部服务。

用法：
    python scripts/test_pipeline.py

每个 URL 在 tmp/ 下生成四个文件：
    <safe_name>_stage1.txt  — 清洗后正文
    <safe_name>_stage2.txt  — 分段 + RST 关系
    <safe_name>_stage3.txt  — 本体对齐标签
    <safe_name>_stage4.txt  — 三元组事实 + 证据
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

class FakeApp:
    store         = fake_postgres
    crawler_store = fake_crawler_postgres
    objects       = objects
    ontology      = registry
    llm           = llm

app = FakeApp()

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
from semcore.core.context import PipelineContext

import httpx

stage1 = IngestStage()
stage2 = SegmentStage()
stage3 = AlignStage()
stage4 = ExtractStage()

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

    print(f"  Stage3: tags={len(all_tags)}  dist={dict(tag_type_dist)}  "
          f"canonical_segs={segs_with_canonical}/{len(segs)}  candidates={len(candidates)}")

    s3_lines = [
        f"[{rank}] {label}",
        f"标签总数: {len(all_tags)}  类型: {dict(tag_type_dist)}",
        f"canonical覆盖: {segs_with_canonical}/{len(segs)} 段",
        f"候选词: {len(candidates)}",
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
        # full segment text
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

    facts = fetchall("SELECT * FROM facts ORDER BY confidence DESC", ())
    evidence = fetchall(
        "SELECT e.* FROM evidence e WHERE e.source_doc_id = ?", (doc_id,)
    )
    ev_by_fact: dict = defaultdict(list)
    for e in evidence:
        ev_by_fact[e["fact_id"]].append(e)
    ev_method = {e["fact_id"]: e.get("extraction_method", "?") for e in evidence}
    method_dist = Counter(ev_method.get(f["fact_id"], "?") for f in facts)

    print(f"  Stage4: facts={len(facts)}  methods={dict(method_dist)}  evidence={len(evidence)}")

    s4_lines = [
        f"[{rank}] {label}",
        f"三元组: {len(facts)}  方法: {dict(method_dist)}  evidence: {len(evidence)}",
        "",
        "── Facts ──",
    ]
    for f in facts:
        method = ev_method.get(f["fact_id"], "?")
        s4_lines.append(
            f"  [{method:<12}] conf={f.get('confidence', 0):.3f}  "
            f"{f['subject']}  ──[{f['predicate']}]──>  {f['object']}"
        )
        evs = ev_by_fact.get(f["fact_id"], [])
        for e in evs:
            s4_lines.append(
                f"              ← seg={str(e.get('segment_id',''))[:12]}  "
                f"rank={e.get('source_rank','?')}  score={e.get('evidence_score', 0):.3f}"
            )
    (ROOT / "tmp" / f"{safe}_stage4.txt").write_text("\n".join(s4_lines), encoding="utf-8")

    summary_rows.append({
        "label": label, "rank": rank,
        "segs": len(segs), "tokens": total_tokens,
        "rst": len(rst_relations), "types": dict(type_dist),
        "tags": len(all_tags), "candidates": len(candidates),
        "facts": len(facts), "methods": dict(method_dist),
    })

    # ── 清理：重置 fake DB 中本文档的数据，避免下一个 URL 污染 ─────────────────
    for tbl in ("facts", "evidence", "segment_tags", "segments"):
        try:
            if tbl in ("facts", "evidence"):
                # facts/evidence 没有 source_doc_id 直接关联，通过 evidence 删
                pass
            execute(f"DELETE FROM {tbl} WHERE source_doc_id = ?", (doc_id,)) \
                if tbl in ("segments",) else None
        except Exception:
            pass
    # 更干净的方法：只清 segments 和 tags（facts 跨文档比较本来就需要保留）
    execute("DELETE FROM segment_tags WHERE segment_id IN "
            "(SELECT segment_id FROM segments WHERE source_doc_id = ?)", (doc_id,))
    execute("DELETE FROM segments WHERE source_doc_id = ?", (doc_id,))
    execute("DELETE FROM facts", ())
    execute("DELETE FROM evidence", ())
    execute("DELETE FROM evolution_candidates", ())

# ══════════════════════════════════════════════════════════════════════════════
# 汇总表
# ══════════════════════════════════════════════════════════════════════════════
print(f"\n{SEP}")
print("  汇总")
print(SEP)
print(f"  {'label':<25} {'rank'} {'segs':>5} {'tok':>6} {'rst':>4} {'tags':>5} {'cand':>5} {'facts':>6}")
print(f"  {'-'*25} {'-'*4} {'-'*5} {'-'*6} {'-'*4} {'-'*5} {'-'*5} {'-'*6}")
for r in summary_rows:
    if "error" in r:
        print(f"  {r['label']:<25} [{r['rank']}]  ERROR: {r['error']}")
    else:
        print(
            f"  {r['label']:<25} [{r['rank']}]"
            f"  {r['segs']:>4}  {r['tokens']:>5}  {r['rst']:>3}"
            f"  {r['tags']:>4}  {r['candidates']:>4}  {r['facts']:>5}"
        )

print(f"\n  输出目录: {ROOT / 'tmp'}/")
print(f"  文件命名: <label>_stage{{1..4}}.txt")
print(SEP)
