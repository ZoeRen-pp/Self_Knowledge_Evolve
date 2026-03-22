"""Stage 2: Semantic segmentation — rules S1-S4."""

from __future__ import annotations

import logging
import re
import uuid

from src.db.postgres import fetchone, fetchall, execute, get_conn
from src.utils.text import token_count, sliding_window_split
from src.utils.hashing import simhash

log = logging.getLogger(__name__)

# Rule S2: semantic role keyword patterns
_ROLE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r'\b(is defined as|refers to|is a type of|means that|definition of)\b', re.I), "definition"),
    (re.compile(r'\b(works by|mechanism|algorithm|process of|how it)\b', re.I), "mechanism"),
    (re.compile(r'\b(must|shall|required|mandatory|limitation|constraint|not allowed)\b', re.I), "constraint"),
    (re.compile(r'\b(configure|configuration|set the|enable|disable|command)\b', re.I), "config"),
    (re.compile(r'\b(fault|failure|error|alarm|down state|outage|flap)\b', re.I), "fault"),
    (re.compile(r'\b(troubleshoot|debug|diagnose|verify|check the log)\b', re.I), "troubleshooting"),
    (re.compile(r'\b(best practice|recommendation|suggested|advised|tip)\b', re.I), "best_practice"),
    (re.compile(r'\b(performance|throughput|latency|bandwidth|packet loss|delay)\b', re.I), "performance"),
    (re.compile(r'\b(compared to|versus|vs\.|difference between|unlike)\b', re.I), "comparison"),
    (re.compile(r'^(\s*\|.+\|)', re.M), "table"),
    (re.compile(r'(```|^    \S)', re.M), "code"),
]

_HEADING_RE = re.compile(r'^(#{1,4})\s+(.+)', re.M)
_TABLE_RE   = re.compile(r'^\s*\|.+\|', re.M)
_CODE_RE    = re.compile(r'```[\s\S]*?```|^( {4}|\t)\S.+', re.M)
_CONFIG_RE  = re.compile(r'^[\w\-]+[>#]\s+\S', re.M)


class SegmentStage:
    def process(self, source_doc_id: str) -> list[dict]:
        doc = fetchone(
            "SELECT * FROM documents WHERE source_doc_id = %s", (source_doc_id,)
        )
        if not doc:
            log.error("Document %s not found", source_doc_id)
            return []

        # Stub: load clean text from object storage
        clean_text = self._load_clean_text(doc)
        if not clean_text:
            return []

        raw_segments = self._segment_document(clean_text, doc.get("doc_type", "tech_article"))
        saved: list[dict] = []

        with get_conn() as conn:
            with conn.cursor() as cur:
                for idx, seg in enumerate(raw_segments):
                    seg_id = str(uuid.uuid4())
                    sh = simhash(seg["raw_text"])
                    cur.execute(
                        """
                        INSERT INTO segments (
                            segment_id, source_doc_id, section_path, section_title,
                            segment_index, segment_type, raw_text, normalized_text,
                            token_count, simhash_value, confidence, lifecycle_state
                        ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')
                        ON CONFLICT DO NOTHING
                        """,
                        (
                            seg_id, source_doc_id,
                            seg.get("section_path", []),
                            seg.get("section_title", ""),
                            idx,
                            seg["segment_type"],
                            seg["raw_text"],
                            seg["raw_text"],  # normalized_text = same for now
                            seg["token_count"],
                            sh,
                            0.8,
                        ),
                    )
                    saved.append({**seg, "segment_id": seg_id, "source_doc_id": source_doc_id})

        execute(
            "UPDATE documents SET status='segmented' WHERE source_doc_id=%s", (source_doc_id,)
        )
        execute(
            "INSERT INTO extraction_jobs (job_type, source_doc_id, status, pipeline_version) VALUES ('tagging',%s,'pending','0.1.0')",
            (source_doc_id,),
        )
        log.info("Document %s → %d segments", source_doc_id, len(saved))
        return saved

    # ── Segmentation logic ────────────────────────────────────────

    def _segment_document(self, text: str, doc_type: str) -> list[dict]:
        """Rules S1-S4: structural split then semantic refinement."""
        # Step 1: structural split by headings
        raw_chunks = self._structural_split(text)
        # Step 2: semantic role + length control per chunk
        segments: list[dict] = []
        for chunk in raw_chunks:
            sub = self._process_chunk(chunk)
            segments.extend(sub)
        return segments

    def _structural_split(self, text: str) -> list[dict]:
        """Rule S1: split on headings, code blocks, tables."""
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

        for line in text.split('\n'):
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

    def _process_chunk(self, chunk: dict) -> list[dict]:
        """Rule S2 (semantic role) + Rule S3 (length control)."""
        text = chunk["raw_text"]
        tc = token_count(text)
        seg_type = self._classify_semantic_role(text)

        # Rule S3: too short → will be merged by caller (just mark it)
        if tc < 30:
            return []  # drop tiny fragments

        # Rule S3: too long → sliding window split
        if tc > 1024:
            windows = sliding_window_split(text, window=512, overlap=64)
            return [
                {**chunk, "raw_text": w, "segment_type": seg_type, "token_count": token_count(w)}
                for w in windows
            ]

        return [{**chunk, "segment_type": seg_type, "token_count": tc}]

    def _classify_semantic_role(self, text: str) -> str:
        """Rule S2: match keyword patterns to assign segment type."""
        sample = text[:600]
        for pattern, role in _ROLE_PATTERNS:
            if pattern.search(sample):
                return role
        return "unknown"

    def _load_clean_text(self, doc: dict) -> str:
        """Stub: returns placeholder text. Replace with real object storage fetch."""
        return (
            "# BGP Overview\n"
            "BGP is defined as a path-vector routing protocol used for inter-AS routing.\n"
            "## Configuration\n"
            "To configure BGP, you must enable the BGP process and configure neighbors.\n"
            "## Fault\n"
            "BGP session failure can be caused by TCP port 179 being blocked.\n"
        )
