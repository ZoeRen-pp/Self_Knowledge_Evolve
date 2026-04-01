"""Stage 2: Semantic segmentation - rules S1-S4 + EDU/RST population."""

from __future__ import annotations

import json
import logging
import re
import uuid

from semcore.core.context import PipelineContext
from semcore.pipeline.base import Stage
from semcore.providers.base import ObjectStore, RelationalStore

from src.utils.text import token_count, sliding_window_split
from src.utils.hashing import simhash
from src.utils.llm_extract import LLMExtractor, RST_RELATION_TYPES

log = logging.getLogger(__name__)

# Rule-based RST fallback: (src_segment_type, dst_segment_type) -> relation_type
# Uses the 20-type universal RST taxonomy (see RST_RELATION_TYPES in llm_extract.py)
_RULE_RST: dict[tuple[str, str], str] = {
    # definition → X
    ("definition",      "definition"):      "Joint",
    ("definition",      "mechanism"):       "Explanation",
    ("definition",      "config"):          "Enablement",
    ("definition",      "constraint"):      "Background",
    # mechanism → X
    ("mechanism",       "mechanism"):       "Joint",
    ("mechanism",       "config"):          "Means",
    ("mechanism",       "constraint"):      "Condition",
    ("mechanism",       "troubleshooting"): "Problem-Solution",
    ("mechanism",       "best_practice"):   "Justification",
    ("mechanism",       "performance"):     "Cause-Result",
    # config → X
    ("config",          "config"):          "Joint",
    ("config",          "troubleshooting"): "Problem-Solution",
    ("config",          "best_practice"):   "Evaluation",
    ("config",          "constraint"):      "Condition",
    ("config",          "mechanism"):       "Purpose",
    # constraint → X
    ("constraint",      "config"):          "Condition",
    ("constraint",      "constraint"):      "Joint",
    ("constraint",      "troubleshooting"): "Enablement",
    ("constraint",      "best_practice"):   "Concession",
    # fault → X
    ("fault",           "troubleshooting"): "Problem-Solution",
    ("fault",           "mechanism"):       "Cause-Result",
    ("fault",           "fault"):           "Joint",
    ("fault",           "config"):          "Result-Cause",
    # troubleshooting → X
    ("troubleshooting", "best_practice"):   "Justification",
    ("troubleshooting", "config"):          "Means",
    ("troubleshooting", "troubleshooting"): "Sequence",
    # best_practice → X
    ("best_practice",   "best_practice"):   "Joint",
    ("best_practice",   "config"):          "Means",
    ("best_practice",   "constraint"):      "Concession",
    # performance → X
    ("performance",     "comparison"):      "Contrast",
    ("performance",     "performance"):     "Joint",
    ("performance",     "config"):          "Cause-Result",
    # comparison → X
    ("comparison",      "best_practice"):   "Evaluation",
    ("comparison",      "comparison"):      "Joint",
    # code / table → X (structural)
    ("code",            "code"):            "Joint",
    ("table",           "table"):           "Joint",
    ("code",            "definition"):      "Evidence",
    ("table",           "definition"):      "Evidence",
}

# Rule S2: semantic role keyword patterns
_ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(is defined as|refers to|is a type of|means that|definition of"
                r"|header format|field.{0,20}(?:contain|indicate|specif)"
                r"|data unit|segment format|datagram format|packet format)\b", re.I), "definition"),
    (re.compile(r"\b(works by|mechanism|algorithm|process of|how it"
                r"|state machine|handshake|retransmit|acknowledgment|three.way"
                r"|sliding window|flow control|congestion|encapsulat|multiplexing"
                r"|demultiplexing|reassembly|fragmentation)\b", re.I), "mechanism"),
    (re.compile(r"\b(must|shall|required|mandatory|limitation|constraint|not allowed"
                r"|must not|shall not)\b", re.I), "constraint"),
    (re.compile(r"\b(configure|configuration|set the|enable|disable|command)\b", re.I), "config"),
    (re.compile(r"\b(fault|failure|error|alarm|down state|outage|flap"
                r"|reset|abort|refuse|reject|timeout)\b", re.I), "fault"),
    (re.compile(r"\b(troubleshoot|debug|diagnose|verify|check the log)\b", re.I), "troubleshooting"),
    (re.compile(r"\b(best practice|recommendation|suggested|advised|tip"
                r"|security consideration)\b", re.I), "best_practice"),
    (re.compile(r"\b(performance|throughput|latency|bandwidth|packet loss|delay"
                r"|maximum segment size|MSS|window size|round.trip|RTT"
                r"|retransmission timeout|RTO|timer)\b", re.I), "performance"),
    (re.compile(r"\b(compared to|versus|vs\.|difference between|unlike)\b", re.I), "comparison"),
    (re.compile(r"^(\s*\|.+\|)", re.M), "table"),
    (re.compile(r"(```|^    \S)", re.M), "code"),
    (re.compile(r"\+-+-\+-\+|[\+\-]{5,}\+", re.M), "definition"),  # ASCII diagrams in RFCs
]

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)", re.M)
_RFC_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s{2,}([A-Z].*)", re.M)
_ALLCAPS_TITLE_RE = re.compile(r"^([A-Z][A-Z \-]{4,})$", re.M)
_BLANK_BLOCK_RE = re.compile(r"\n{3,}")
_TABLE_RE = re.compile(r"^\s*\|.+\|", re.M)
_CODE_RE = re.compile(r"```[\s\S]*?```|^( {4}|\t)\S.+", re.M)
_CONFIG_RE = re.compile(r"^[\w\-]+[>#]\s+\S", re.M)


class SegmentStage(Stage):
    name = "segment"

    def __init__(self) -> None:
        self.llm = LLMExtractor()
        self._objects: ObjectStore | None = None
        self._store: RelationalStore | None = None

    def process(self, ctx: PipelineContext, app) -> PipelineContext:  # type: ignore[override]
        self._objects = getattr(app, "objects", None)
        self._store = app.store
        self._crawler_store = getattr(app, "crawler_store", None) or app.store
        if hasattr(app, "llm"):
            if hasattr(app.llm, "is_enabled"):
                self.llm = app.llm
        source_doc_id = ctx.doc.source_doc_id if ctx.doc else ctx.source_doc_id
        segs = self._run(source_doc_id)
        self.set_output(ctx, {"segments": segs})
        return ctx

    def _run(self, source_doc_id: str) -> list[dict]:
        store = self._store
        doc = store.fetchone(
            "SELECT * FROM documents WHERE source_doc_id = %s", (source_doc_id,)
        )
        if not doc:
            log.error("Document %s not found", source_doc_id)
            return []

        clean_text = self._load_clean_text(doc)
        if not clean_text:
            return []

        raw_segments = self._segment_document(clean_text, doc.get("doc_type", "tech_article"))
        saved: list[dict] = []

        content_source = self._make_content_source(doc)

        with store.transaction() as cur:
            for idx, seg in enumerate(raw_segments):
                seg_id = str(uuid.uuid4())
                sh = simhash(seg["raw_text"])
                title = self._extract_title(seg)
                cur.execute(
                    """
                    INSERT INTO segments (
                        segment_id, source_doc_id, section_path, section_title,
                        segment_index, segment_type, raw_text, normalized_text,
                        token_count, simhash_value, confidence, lifecycle_state,
                        title, content_source
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active',%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        seg_id, source_doc_id,
                        seg.get("section_path", []),
                        seg.get("section_title", ""),
                        idx,
                        seg["segment_type"],
                        seg["raw_text"],
                        seg["raw_text"],
                        seg["token_count"],
                        sh,
                        seg.get("confidence", 0.7),
                        title,
                        content_source,
                    ),
                )
                saved.append({
                    **seg,
                    "segment_id":    seg_id,
                    "source_doc_id": source_doc_id,
                    "segment_type":  seg["segment_type"],
                })

        rst_count = self._insert_rst_relations(saved)

        store.execute(
            "UPDATE documents SET status='segmented' WHERE source_doc_id=%s", (source_doc_id,)
        )
        self._crawler_store.execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version)"
            " VALUES ('tagging',%s,'pending','0.2.0')",
            (source_doc_id,),
        )
        segment_ids = [seg["segment_id"] for seg in saved]
        id_preview = self._preview_ids(segment_ids)
        log.info(
            "Segmented doc=%s segments=%d rst_relations=%d segment_ids=%s",
            source_doc_id,
            len(saved),
            rst_count,
            id_preview,
        )
        return saved

    def _segment_document(self, text: str, doc_type: str) -> list[dict]:
        """Rules S1-S4: structural split then semantic refinement."""
        raw_chunks = self._structural_split(text)
        segments: list[dict] = []
        for chunk in raw_chunks:
            sub = self._process_chunk(chunk)
            segments.extend(sub)
        return segments

    def _structural_split(self, text: str) -> list[dict]:
        """Rule S1: split on headings. Auto-detects markdown vs RFC/plain-text."""
        if _HEADING_RE.search(text):
            return self._split_markdown(text)
        if _RFC_SECTION_RE.search(text) or _ALLCAPS_TITLE_RE.search(text):
            return self._split_rfc(text)
        return self._split_plaintext(text)

    def _split_markdown(self, text: str) -> list[dict]:
        """Split on markdown headings (# / ## / ### / ####)."""
        chunks: list[dict] = []
        current_path: list[str] = []
        current_title = ""
        buf: list[str] = []

        def flush():
            content = "\n".join(buf).strip()
            if content:
                chunks.append({
                    "section_path":  list(current_path),
                    "section_title": current_title,
                    "raw_text":      content,
                })
            buf.clear()

        for line in text.split("\n"):
            m = _HEADING_RE.match(line)
            if m:
                flush()
                level = len(m.group(1))
                title = m.group(2).strip()
                current_path = current_path[:level - 1] + [title]
                current_title = title
            else:
                buf.append(line)

        flush()
        return chunks

    def _split_rfc(self, text: str) -> list[dict]:
        """Split on RFC-style numbered sections and ALL-CAPS titles."""
        chunks: list[dict] = []
        current_path: list[str] = []
        current_title = ""
        buf: list[str] = []

        def flush():
            content = "\n".join(buf).strip()
            if content:
                chunks.append({
                    "section_path":  list(current_path),
                    "section_title": current_title,
                    "raw_text":      content,
                })
            buf.clear()

        for line in text.split("\n"):
            if "\f" in line:
                continue  # skip RFC page-break lines

            m = _RFC_SECTION_RE.match(line)
            if m:
                flush()
                level = m.group(1).count(".") + 1
                title = m.group(2).strip()
                current_path = current_path[:level - 1] + [title]
                current_title = title
                continue

            m2 = _ALLCAPS_TITLE_RE.match(line.strip())
            if m2 and len(line.strip()) > 4:
                flush()
                current_title = m2.group(1).strip().title()
                current_path = [current_title]
                continue

            buf.append(line)

        flush()
        return chunks

    def _split_plaintext(self, text: str) -> list[dict]:
        """Fallback: split on triple-blank-lines or form-feeds."""
        import re as _re
        parts = _re.split(r"\f", text)
        if len(parts) <= 1:
            parts = _BLANK_BLOCK_RE.split(text)
        return [
            {
                "section_path": [],
                "section_title": p.strip().split("\n", 1)[0].strip()[:80],
                "raw_text": p.strip(),
            }
            for p in parts if p.strip()
        ]

    def _process_chunk(self, chunk: dict) -> list[dict]:
        """Rule S2 (semantic role) + Rule S3 (length control)."""
        text = chunk["raw_text"]
        tc = token_count(text)
        seg_type = self._classify_semantic_role(text)

        if tc < 30:
            return []

        conf = self._estimate_confidence(text, tc, seg_type)

        if tc > 1024:
            windows = sliding_window_split(text, window=512, overlap=64)
            return [
                {**chunk, "raw_text": w, "segment_type": seg_type,
                 "token_count": token_count(w), "confidence": conf}
                for w in windows
            ]

        return [{**chunk, "segment_type": seg_type, "token_count": tc, "confidence": conf}]

    @staticmethod
    def _estimate_confidence(text: str, tc: int, seg_type: str) -> float:
        """Heuristic confidence based on segment quality signals."""
        conf = 0.7  # base

        # Length: very short or very long segments are less reliable
        if 50 <= tc <= 512:
            conf += 0.10  # sweet spot
        elif tc > 512:
            conf += 0.05  # acceptable but may be noisy

        # Known semantic role is better than unknown
        if seg_type != "unknown":
            conf += 0.10

        # Has section title context
        # (caller adds section_title to chunk, but we don't have it here;
        #  this is handled at save time)

        # Rich content signals (has technical terms, not just boilerplate)
        upper_ratio = sum(1 for c in text if c.isupper()) / max(len(text), 1)
        if 0.02 < upper_ratio < 0.3:
            conf += 0.05  # likely has acronyms/technical terms

        return round(min(conf, 1.0), 2)

    def _classify_semantic_role(self, text: str) -> str:
        """Rule S2: match keyword patterns to assign segment type."""
        sample = text[:1500]
        for pattern, role in _ROLE_PATTERNS:
            if pattern.search(sample):
                return role
        return "unknown"

    def _load_clean_text(self, doc: dict) -> str:
        """Load cleaned text from object storage or fallback stub."""
        uri = doc.get("cleaned_storage_uri") or ""
        if uri.startswith("minio://") and self._objects is not None:
            try:
                raw = self._objects.get(uri).decode("utf-8", errors="replace")
                log.info("Loaded cleaned text: doc=%s uri=%s bytes=%s", doc.get("source_doc_id"), uri, len(raw))
                return raw
            except Exception as exc:
                log.error("Failed to load cleaned text: doc=%s uri=%s err=%s", doc.get("source_doc_id"), uri, exc)
        return (
            "# BGP Overview\n"
            "BGP is defined as a path-vector routing protocol used for inter-AS routing.\n"
            "## Configuration\n"
            "To configure BGP, you must enable the BGP process and configure neighbors.\n"
            "## Fault\n"
            "BGP session failure can be caused by TCP port 179 being blocked.\n"
        )

    def _extract_title(self, seg: dict) -> str:
        """Generate a title for the EDU: LLM if available, else section_title, else first sentence."""
        llm_title = self.llm.generate_title(seg["raw_text"])
        if llm_title:
            return llm_title[:255]
        if seg.get("section_title"):
            return seg["section_title"][:255]
        first = seg["raw_text"].strip().split(".")[0].strip()
        return (first or seg["raw_text"][:80])[:255]

    def _make_content_source(self, doc: dict) -> str:
        """Return '{site_key}:{canonical_url}' for segments.content_source."""
        site_key = doc.get("site_key") or ""
        url = doc.get("canonical_url") or doc.get("source_url") or ""
        return f"{site_key}:{url}"[:128]

    def _insert_rst_relations(self, segments: list[dict]) -> int:
        """Insert RST relations between adjacent EDU pairs into t_rst_relation."""
        if len(segments) < 2:
            return 0

        pairs: list[tuple[str, str, str, str]] = [
            (segments[i]["segment_id"],   segments[i]["raw_text"],
             segments[i + 1]["segment_id"], segments[i + 1]["raw_text"])
            for i in range(len(segments) - 1)
        ]

        llm_enabled = self.llm.is_enabled()
        if llm_enabled:
            relation_types = self.llm.extract_rst_relations(pairs)
        else:
            relation_types = [
                _RULE_RST.get(
                    (segments[i].get("segment_type", ""), segments[i + 1].get("segment_type", "")),
                    "Sequence",
                )
                for i in range(len(pairs))
            ]

        rows = []
        for i, (src_id, _, dst_id, _) in enumerate(pairs):
            rel_type = relation_types[i] if i < len(relation_types) else "Sequence"
            src_type = segments[i].get("segment_type", "unknown")
            dst_type = segments[i + 1].get("segment_type", "unknown")
            source = "llm" if llm_enabled else "rule"
            rows.append((
                str(uuid.uuid4()),
                rel_type,
                src_id,
                dst_id,
                json.dumps({"SYNTACTIC_ORDER": i, "src_type": src_type, "dst_type": dst_type}),
                source,
            ))

        if not rows:
            return 0

        store = self._store
        with store.transaction() as cur:
            for row in rows:
                cur.execute(
                    """
                    INSERT INTO t_rst_relation
                        (nn_relation_id, relation_type, src_edu_id, dst_edu_id,
                         meta_context, relation_source)
                    VALUES (%s, %s, %s, %s, %s::jsonb, %s)
                    ON CONFLICT (nn_relation_id) DO NOTHING
                    """,
                    row,
                )
        return len(rows)

    @staticmethod
    def _preview_ids(values: list[str], limit: int = 8) -> str:
        if not values:
            return "[]"
        if len(values) <= limit:
            return "[" + ", ".join(values) + "]"
        return "[" + ", ".join(values[:limit]) + f", ...(+{len(values) - limit})]"