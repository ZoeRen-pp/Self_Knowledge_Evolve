"""
Stage 1 + Stage 2 (Segment) 效果测试 — 内存模式。

用法：
    python scripts/test_stage2.py

输出：
  - 每个文档的全量分段列表（含类型/token数/置信度/章节路径/文本摘要）
  - 统计：各 segment_type 分布、被 _process_chunk 丢弃的过短块
  - tmp/stage2_<name>.txt — 每个文档的完整分段明细
"""

from __future__ import annotations

import re
import sys
import uuid
import logging
from collections import Counter
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "semcore"))

# 只看关键阶段的 INFO，屏蔽其他噪音
logging.basicConfig(level=logging.WARNING, format="%(levelname)s  %(name)s  %(message)s")
for name in (
    "src.pipeline.stages.stage1_ingest",
    "src.pipeline.stages.stage2_segment",
    "src.pipeline.preprocessing.extractor",
    "src.utils.llm_extract",
):
    logging.getLogger(name).setLevel(logging.DEBUG)

# ── 1. 注入 fake db ───────────────────────────────────────────────────────────
import types
from src.dev import fake_postgres, fake_neo4j, fake_crawler_postgres

_db_mod = types.ModuleType("src.db")
_db_mod.postgres = fake_postgres
_db_mod.neo4j_client = fake_neo4j
_db_mod.crawler_postgres = fake_crawler_postgres
_db_mod.health_check = lambda: {}

sys.modules["src.db"]                  = _db_mod
sys.modules["src.db.postgres"]         = fake_postgres
sys.modules["src.db.neo4j_client"]     = fake_neo4j
sys.modules["src.db.crawler_postgres"] = fake_crawler_postgres

# ── 2. 内存对象存储 ────────────────────────────────────────────────────────────
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

# ── 3. 检查 LLM 是否可用（影响 noise 分类） ──────────────────────────────────
try:
    from src.config.settings import settings
    llm_on = settings.LLM_ENABLED and bool(settings.LLM_API_KEY)
    print(f"[诊断] LLM_ENABLED={settings.LLM_ENABLED}  LLM_API_KEY={'(set)' if settings.LLM_API_KEY else '(empty)'}  LLM_MODEL={settings.LLM_MODEL}  LLM_BASE_URL={settings.LLM_BASE_URL}")
except Exception as e:
    llm_on = False
    print(f"[诊断] settings 加载失败: {e}")

from src.utils.llm_extract import LLMExtractor, _SEGTYPE_SYSTEM_PROMPT, _SEGTYPE_USER_TEMPLATE
_diag_llm = LLMExtractor()
print(f"[诊断] LLMExtractor.is_enabled()={_diag_llm.is_enabled()}")

# 最小探针：打印 LLM 原始返回
_probe_prompt = _SEGTYPE_USER_TEMPLATE.format(
    segments_text="Segment 0:\n  Title: BGP Overview\n  Text: BGP is an exterior gateway protocol used to exchange routing information between autonomous systems. It uses TCP port 179."
)
_raw_resp = _diag_llm._call_llm(_SEGTYPE_SYSTEM_PROMPT, _probe_prompt, max_tokens=200)
print(f"[诊断] LLM 原始返回: {_raw_resp!r}")

# ── 4. 待测 URL ───────────────────────────────────────────────────────────────
TEST_URLS = [
    ("S", "https://datatracker.ietf.org/doc/html/rfc4271"),
    ("S", "https://www.3gpp.org/specifications-groups/sa-plenary/sa-wg2-arch"),
    ("A", "https://www.juniper.net/documentation/us/en/software/junos/bgp/topics/topic-map/bgp-overview.html"),
]

# ── 5. 构造最简 app ────────────────────────────────────────────────────────────
class _FakeApp:
    store = fake_postgres
    crawler_store = fake_crawler_postgres
    objects = objects

# ── 6. 阶段实例 ───────────────────────────────────────────────────────────────
import httpx
import hashlib
from src.pipeline.stages.stage1_ingest import IngestStage
from src.pipeline.stages.stage2_segment import SegmentStage
from src.dev.fake_postgres import fetchone, execute, fetchall
from semcore.core.context import PipelineContext

stage1 = IngestStage()
stage2 = SegmentStage()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SEP  = "=" * 72
SEP2 = "─" * 72

(ROOT / "tmp").mkdir(exist_ok=True)

print()
print(SEP)
print("  STAGE 1 + 2 — 分段效果测试")
print(f"  LLM noise分类: {'已启用（segment_type=noise 段落将被丢弃）' if llm_on else '未启用（所有段落标为 unknown，不丢弃）'}")
print(SEP)

for rank, url in TEST_URLS:
    print(f"\n{SEP2}")
    print(f"  URL  : {url}")
    print(f"  rank : {rank}")

    # 抓取
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=25, follow_redirects=True)
        raw_bytes = resp.content
        http_status = resp.status_code
        final_url = str(resp.url)
    except Exception as exc:
        print(f"  [FETCH ERROR] {exc}")
        continue

    if http_status >= 400:
        print(f"  [SKIP] HTTP {http_status}")
        continue

    # 存入对象存储 + documents 表
    c_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw_uri = objects.put(f"raw/{c_hash}.html", raw_bytes)
    doc_id = str(uuid.uuid4())
    execute(
        """INSERT INTO documents
               (source_doc_id, site_key, source_url, canonical_url,
                source_rank, raw_storage_uri, status, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, 'raw', ?)""",
        (doc_id, "test", url, final_url, rank, raw_uri, c_hash),
    )

    # Stage 1
    ctx = PipelineContext(source_doc_id=doc_id)
    ctx = stage1.process(ctx, _FakeApp())

    doc = fetchone("SELECT * FROM documents WHERE source_doc_id = ?", (doc_id,))
    status = doc.get("status", "?")
    if status not in ("cleaned",):
        print(f"  [Stage1] status={status}，跳过 Stage2")
        continue

    print(f"  [Stage1] status=cleaned  doc_type={doc.get('doc_type')}  title={doc.get('title', '')!r}")

    # Stage 2
    ctx = stage2.process(ctx, _FakeApp())

    # 读取分段结果
    segs = fetchall(
        "SELECT * FROM segments WHERE source_doc_id = ? ORDER BY segment_index",
        (doc_id,),
    )

    type_counts: Counter = Counter(s["segment_type"] for s in segs)
    total_tokens = sum(s["token_count"] for s in segs)

    print(f"  [Stage2] 分段数={len(segs)}  总token={total_tokens}")
    print(f"  segment_type 分布: {dict(type_counts)}")

    # 写出详细报告
    safe_name = re.sub(r"[^\w]", "_", url.split("//")[-1])[:60]
    out_path = ROOT / "tmp" / f"stage2_{safe_name}.txt"

    lines = []
    lines.append(f"URL: {url}")
    lines.append(f"doc_type: {doc.get('doc_type')}  title: {doc.get('title', '')}")
    lines.append(f"分段数: {len(segs)}  总tokens: {total_tokens}")
    lines.append(f"type分布: {dict(type_counts)}")
    lines.append("")

    for seg in segs:
        path_str = " > ".join(seg.get("section_path") or []) if seg.get("section_path") else ""
        lines.append(f"{'─'*60}")
        lines.append(f"[{seg['segment_index']:03d}] type={seg['segment_type']:<18} "
                     f"tokens={seg['token_count']:<5} conf={seg.get('confidence', 0):.2f}")
        if path_str:
            lines.append(f"     path : {path_str}")
        if seg.get("section_title"):
            lines.append(f"     title: {seg['section_title']}")
        # 正文全文（缩进4空格）
        full_text = (seg.get("raw_text") or "").strip()
        for line in full_text.splitlines():
            lines.append(f"    {line}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  [已写出] {out_path}")

    # 控制台也打印前 15 段预览
    print(f"\n  前15段预览：")
    for seg in segs[:15]:
        path_str = (" > ".join(seg.get("section_path") or [])) if seg.get("section_path") else ""
        preview = (seg.get("raw_text") or "").replace("\n", " ").strip()[:80]
        print(f"  [{seg['segment_index']:03d}] {seg['segment_type']:<18} "
              f"tok={seg['token_count']:<5} conf={seg.get('confidence', 0):.2f} "
              f"| {path_str[:30]:<30} | {preview}")

    if len(segs) > 15:
        print(f"  ... 剩余 {len(segs) - 15} 段见 {out_path.name}")

print(f"\n{SEP}")
print("  测试完成")
print(SEP)
