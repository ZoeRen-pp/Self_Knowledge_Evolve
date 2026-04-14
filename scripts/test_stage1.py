"""
Stage 1 (Ingest) 效果测试脚本 — 内存模式，无需任何外部服务。

用法：
    python scripts/test_stage1.py

流程：
  1. 注入 fake db 模块
  2. 用 httpx 真实抓取目标 URL 的 HTML
  3. 把 HTML 存入内存对象存储
  4. 写入 documents 记录（status='raw'）
  5. 运行 Stage 1（IngestStage）
  6. 打印：原始 URL、清洗后摘要、质量信号、doc_type、token 数
"""

from __future__ import annotations

import re
import sys
import uuid
import logging
from pathlib import Path

# 强制 stdout/stderr 使用 UTF-8（Windows 控制台默认 cp936 会乱码）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── 0. 路径 & 日志 ────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "semcore"))

logging.basicConfig(
    level=logging.WARNING,          # 只打 WARNING 以上，避免 INFO 噪音
    format="%(levelname)s  %(name)s  %(message)s",
)
# Stage1 和 extractor 的 INFO 级别日志单独开放，方便观察质量判断过程
logging.getLogger("src.pipeline.stages.stage1_ingest").setLevel(logging.INFO)
logging.getLogger("src.pipeline.preprocessing.extractor").setLevel(logging.INFO)

# ── 1. 注入 fake db ───────────────────────────────────────────────────────────
import types
from src.dev import fake_postgres, fake_neo4j, fake_crawler_postgres

_db_mod = types.ModuleType("src.db")
_db_mod.postgres = fake_postgres
_db_mod.neo4j_client = fake_neo4j
_db_mod.crawler_postgres = fake_crawler_postgres
_db_mod.health_check = lambda: {"postgres": True, "neo4j": True}

sys.modules["src.db"]                  = _db_mod
sys.modules["src.db.postgres"]         = fake_postgres
sys.modules["src.db.neo4j_client"]     = fake_neo4j
sys.modules["src.db.crawler_postgres"] = fake_crawler_postgres

# ── 2. 内存对象存储（替代 MinIO）───────────────────────────────────────────────
from semcore.providers.base import ObjectStore

class MemObjectStore(ObjectStore):
    def __init__(self):
        self._data: dict[str, bytes] = {}

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        uri = f"minio://{key}"
        self._data[uri] = data
        return uri

    def get(self, uri: str) -> bytes:
        return self._data[uri]

    def exists(self, uri: str) -> bool:
        return uri in self._data

objects = MemObjectStore()

# ── 3. 待测 URL 列表 ──────────────────────────────────────────────────────────
TEST_URLS = [
    # IETF RFC 4271 — BGP-4 规范，HTML 版（IETF datatracker）
    ("S", "https://datatracker.ietf.org/doc/html/rfc4271"),
    # IETF RFC 7348 — VXLAN，plain text 格式
    ("S", "https://www.rfc-editor.org/rfc/rfc7348.txt"),
    # 3GPP 规范页（HTML）
    ("S", "https://www.3gpp.org/specifications-groups/sa-plenary/sa-wg2-arch"),
    # Juniper BGP 配置文档
    ("A", "https://www.juniper.net/documentation/us/en/software/junos/bgp/topics/topic-map/bgp-overview.html"),
    # Nokia 技术白皮书（HTML，通常可访问）
    ("B", "https://documentation.nokia.com/sr/24-3/books/layer-3-services/configuring-bgp.html"),
]

# ── 4. 抓取 + 注入 + 运行 Stage 1 ────────────────────────────────────────────
import httpx
from src.pipeline.stages.stage1_ingest import IngestStage
from src.dev.fake_postgres import fetchone, execute

stage1 = IngestStage()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

SEPARATOR = "=" * 72

print()
print(SEPARATOR)
print("  STAGE 1 INGEST — 测试报告")
print(SEPARATOR)

for rank, url in TEST_URLS:
    print(f"\n{'─'*72}")
    print(f"  URL   : {url}")
    print(f"  rank  : {rank}")

    # 抓取 HTML
    try:
        resp = httpx.get(url, headers=HEADERS, timeout=20, follow_redirects=True)
        raw_bytes = resp.content
        final_url = str(resp.url)
        http_status = resp.status_code
    except Exception as exc:
        print(f"  [FETCH ERROR] {exc}")
        continue

    print(f"  status: {http_status}  final_url: {final_url}")

    if http_status >= 400:
        print(f"  [SKIP] HTTP {http_status}")
        continue

    # 存入内存对象存储
    import hashlib
    c_hash = hashlib.sha256(raw_bytes).hexdigest()
    raw_key = f"raw/{c_hash}.html"
    raw_uri = objects.put(raw_key, raw_bytes)

    # 写入 documents 记录
    doc_id = str(uuid.uuid4())
    execute(
        """INSERT INTO documents
               (source_doc_id, site_key, source_url, canonical_url,
                source_rank, raw_storage_uri, status, content_hash)
           VALUES (?, ?, ?, ?, ?, ?, 'raw', ?)""",
        (doc_id, "test", url, final_url, rank, raw_uri, c_hash),
    )

    # 构造最简 app 对象（Stage 1 只需要 store 和 objects）
    class _FakeApp:
        store = fake_postgres
        objects = objects

    from semcore.core.context import PipelineContext
    ctx = PipelineContext(source_doc_id=doc_id)
    result_ctx = stage1.process(ctx, _FakeApp())

    # 读取处理后的 document 记录
    doc = fetchone("SELECT * FROM documents WHERE source_doc_id = ?", (doc_id,))

    status    = doc.get("status", "?")
    doc_type  = doc.get("doc_type", "?")
    title     = doc.get("title", "")
    language  = doc.get("language", "?")
    cleaned_uri = doc.get("cleaned_storage_uri") or ""

    print(f"  status  : {status}")
    print(f"  doc_type: {doc_type}")
    print(f"  title   : {title!r}")
    print(f"  language: {language}")

    # 读取清洗后文本
    if cleaned_uri and objects.exists(cleaned_uri):
        clean_text = objects.get(cleaned_uri).decode("utf-8", errors="replace")

        # 写入临时 txt 文件
        safe_name = re.sub(r'[^\w]', '_', url.split("//")[-1])[:60]
        out_path = ROOT / "tmp" / f"stage1_{safe_name}.txt"
        out_path.parent.mkdir(exist_ok=True)
        out_path.write_text(clean_text, encoding="utf-8")
        print(f"  [已写出] {out_path}")

        tokens = len(clean_text.split())

        # 质量信号（重新计算，Stage 1 没有回写到 doc 表）
        from src.pipeline.preprocessing.extractor import _compute_quality_signals, _judge_quality
        sig = _compute_quality_signals(clean_text)
        is_low, reason, _ = _judge_quality(clean_text, tokens)

        print(f"  tokens  : {tokens}")
        print(f"  quality signals:")
        print(f"    sentence_density : {sig['sentence_density']}/1k chars")
        print(f"    listy_ratio      : {sig['listy_ratio']}")
        print(f"    line_count       : {sig['line_count']}")
        print(f"    char_count       : {sig['char_count']}")
        print(f"  low_quality : {is_low}" + (f"  reason: {reason}" if is_low else ""))

        # 打印清洗文本头部和尾部
        lines = [l for l in clean_text.splitlines() if l.strip()]
        head = "\n    ".join(lines[:8])
        tail = "\n    ".join(lines[-4:]) if len(lines) > 12 else ""

        print(f"\n  ── 清洗文本 (前8行) ──")
        print(f"    {head}")
        if tail:
            print(f"    ...")
            print(f"  ── 清洗文本 (后4行) ──")
            print(f"    {tail}")
    elif status == "deduped":
        print("  [已跳过：内容 hash 重复]")
    elif status == "low_quality":
        print("  [已标记为低质量，无清洗文本]")
    else:
        print("  [无清洗文本]")

print(f"\n{SEPARATOR}")
print("  测试完成")
print(SEPARATOR)