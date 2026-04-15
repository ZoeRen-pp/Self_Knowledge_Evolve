"""Stage 2: Semantic segmentation — structural split + LLM-based typing + RST."""

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

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+)", re.M)
_RFC_SECTION_RE = re.compile(r"^(\d+(?:\.\d+)*)\.?\s{2,}([A-Z].*)", re.M)
_ALLCAPS_TITLE_RE = re.compile(r"^([A-Z][A-Z \-]{4,})$", re.M)
_BLANK_BLOCK_RE = re.compile(r"\n{3,}")


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
        if hasattr(app, "llm") and hasattr(app.llm, "classify_segment_types"):
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
        """Joint segmentation + typing in four steps:

        1. Structural split on headings/section markers → section-level chunks
        2. Each chunk split into paragraph-level units (\\n\\n boundaries)
        3. LLM batch-classifies ALL paragraphs in one call → segment_type assigned
        4. Adjacent same-type paragraphs in the same section are merged;
           noise paragraphs are dropped; oversized results are length-controlled.

        This ensures every segment has a single communicative role, and
        segment boundaries coincide with type-change boundaries.
        """
        raw_chunks = self._structural_split(text)
        if not raw_chunks:
            return []

        # Step 1→2: flatten chunks → paragraph units
        all_paras: list[dict] = []
        for chunk in raw_chunks:
            all_paras.extend(self._split_into_paragraphs(chunk))

        if not all_paras:
            return []

        # Step 3: joint LLM classification (one batch for the whole document)
        all_paras = self._classify_paragraphs(all_paras)

        # Step 4a: drop noise
        dropped = sum(1 for p in all_paras if p["segment_type"] == "noise")
        if dropped:
            log.info("Dropped %d noise paragraphs", dropped)
        all_paras = [p for p in all_paras if p["segment_type"] != "noise"]

        # Step 4b: merge adjacent same-type paragraphs within same section
        merged = self._merge_same_type(all_paras)

        # Step 4c: length control + confidence
        result: list[dict] = []
        for seg in merged:
            for sub in self._apply_length_control(seg):
                sub["confidence"] = self._estimate_confidence(
                    sub["raw_text"], sub["token_count"], sub["segment_type"]
                )
                result.append(sub)
        return result

    def _split_into_paragraphs(self, chunk: dict) -> list[dict]:
        """Split a section chunk into paragraph-level units on \\n\\n boundaries.

        Each paragraph becomes a candidate discourse unit. Paragraphs shorter
        than 15 tokens are discarded (likely noise lines or lone headings).
        If the chunk has no paragraph breaks, it is kept as a single unit.
        """
        text = chunk["raw_text"]
        parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not parts:
            parts = [text.strip()] if text.strip() else []

        result: list[dict] = []
        for p in parts:
            tc = token_count(p)
            if tc < 15:
                continue
            result.append({
                **chunk,
                "raw_text":     p,
                "token_count":  tc,
                "segment_type": "unknown",
                "confidence":   0.5,
            })
        return result

    def _classify_paragraphs(self, paras: list[dict]) -> list[dict]:
        """Batch-classify segment_type for all paragraphs (one LLM call).

        When LLM is disabled, all paragraphs keep type 'unknown'.
        """
        if not paras:
            return paras
        types = self.llm.classify_segment_types(paras)
        for para, t in zip(paras, types):
            para["segment_type"] = t
        return paras

    def _merge_same_type(self, paras: list[dict]) -> list[dict]:
        """Merge adjacent paragraphs that share type and section_path.

        Merge conditions (ALL must hold):
        - Same segment_type (and not 'unknown' — avoids over-merging when LLM is off)
        - Same section_path (no cross-heading merging)
        - Merged token count ≤ 1024
        """
        if not paras:
            return []

        result: list[dict] = []
        current = dict(paras[0])

        for para in paras[1:]:
            same_type    = para["segment_type"] == current["segment_type"]
            same_section = para.get("section_path") == current.get("section_path")
            fits          = current["token_count"] + para["token_count"] <= 1024
            not_unknown  = current["segment_type"] != "unknown"

            if same_type and same_section and fits and not_unknown:
                current["raw_text"]    = current["raw_text"] + "\n\n" + para["raw_text"]
                current["token_count"] = current["token_count"] + para["token_count"]
            else:
                result.append(current)
                current = dict(para)

        result.append(current)
        return result

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

    def _apply_length_control(self, seg: dict) -> list[dict]:
        """Split an oversized merged segment while preserving segment_type.

        Segments ≤ 1024 tokens are returned as-is. Larger segments are split
        using the three-level strategy (paragraph → sentence → sliding window).
        The segment_type is inherited by all sub-segments.
        """
        if seg["token_count"] <= 1024:
            return [seg]

        sub_texts = self._split_oversized(seg["raw_text"])
        result: list[dict] = []
        for sub in sub_texts:
            tc = token_count(sub)
            if tc < 15:
                continue
            result.append({**seg, "raw_text": sub, "token_count": tc})
        return result if result else [seg]

    def _split_oversized(self, text: str) -> list[str]:
        """Three-level split for text exceeding 1024 tokens.

        Level 1: paragraph boundaries (\\n\\n)
        Level 2: sentence boundaries (. + space), greedy merge to ~512 tokens
        Level 3: sliding window fallback
        """
        max_tokens = 1024

        # Level 1: split by paragraph boundaries
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if len(paragraphs) > 1 and all(token_count(p) <= max_tokens for p in paragraphs):
            return paragraphs

        # Some paragraphs still too long — try sentence splitting on those
        result = []
        for para in paragraphs:
            if token_count(para) <= max_tokens:
                result.append(para)
            else:
                # Level 2: split by sentence boundaries, greedy merge
                result.extend(self._split_by_sentences(para, target_tokens=512))
        return result

    # Discourse markers that signal a topic shift — force split before these
    _DISCOURSE_MARKERS = re.compile(
        r"^(?:however|therefore|in contrast|furthermore|on the other hand"
        r"|as a result|in conclusion|for example|note that|importantly"
        r"|conversely|similarly|moreover|nevertheless|in addition"
        r"|in summary|consequently|meanwhile|alternatively)\b",
        re.I,
    )

    @classmethod
    def _split_by_sentences(cls, text: str, target_tokens: int = 512) -> list[str]:
        """Split text by sentence boundaries with discourse-marker awareness.

        Forces a chunk break:
          - When accumulated tokens exceed target_tokens
          - At paragraph boundaries (\\n\\n)
          - Before discourse markers signaling a topic shift

        Falls back to sliding window if sentences are still too long.
        """
        # Split on sentence-ending punctuation followed by space/newline,
        # OR on paragraph boundaries (\n\n)
        sentences = re.split(r"(?<=[.!?])\s+|\n\n+", text)
        sentences = [s.strip() for s in sentences if s.strip()]
        if len(sentences) <= 1:
            return sliding_window_split(text, window=target_tokens, overlap=64)

        chunks: list[str] = []
        current: list[str] = []
        current_tc = 0

        for sent in sentences:
            sent_tc = token_count(sent)
            if sent_tc > target_tokens:
                if current:
                    chunks.append(" ".join(current))
                    current, current_tc = [], 0
                chunks.extend(sliding_window_split(sent, window=target_tokens, overlap=64))
                continue

            # Force break before discourse markers (topic shift signal)
            is_topic_shift = bool(cls._DISCOURSE_MARKERS.match(sent))

            if current and (current_tc + sent_tc > target_tokens or is_topic_shift):
                chunks.append(" ".join(current))
                current, current_tc = [], 0

            current.append(sent)
            current_tc += sent_tc

        if current:
            chunks.append(" ".join(current))

        return chunks

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
        """Generate a title for the EDU.

        Priority: section_title → first sentence → LLM (only when no
        section context and segment is long enough to justify an API call).
        This conserves LLM quota for Stage 4 relation extraction.
        """
        if seg.get("section_title"):
            return seg["section_title"][:255]
        first = seg["raw_text"].strip().split(".")[0].strip()
        if first and len(first) > 10:
            return first[:255]
        # Only call LLM when there's no section context and first sentence is too short
        llm_title = self.llm.generate_title(seg["raw_text"])
        if llm_title:
            return llm_title[:255]
        return (first or seg["raw_text"][:80])[:255]

    def _make_content_source(self, doc: dict) -> str:
        """Return '{site_key}:{canonical_url}' for segments.content_source."""
        site_key = doc.get("site_key") or ""
        url = doc.get("canonical_url") or doc.get("source_url") or ""
        return f"{site_key}:{url}"[:128]

    def _insert_rst_relations(self, segments: list[dict]) -> int:
        """Insert paragraph-level discourse relations between adjacent segments."""
        if len(segments) < 2:
            return 0

        pairs: list[tuple[str, str, str, str, str, str]] = [
            (
                segments[i]["segment_id"],     segments[i]["raw_text"],
                segments[i].get("segment_type", "unknown"),
                segments[i + 1]["segment_id"], segments[i + 1]["raw_text"],
                segments[i + 1].get("segment_type", "unknown"),
            )
            for i in range(len(segments) - 1)
        ]

        llm_enabled = self.llm.is_enabled()
        rel_results: list[dict] = (
            self.llm.extract_rst_relations(pairs) if llm_enabled else []
        )

        _default = {"relation_type": "Elaboration", "nuclearity": "NN"}
        rows = []
        for i, (src_id, _, _src_type, dst_id, _, _dst_type) in enumerate(pairs):
            rel = rel_results[i] if i < len(rel_results) else _default
            rel_type   = rel.get("relation_type", "Elaboration")
            nuclearity = rel.get("nuclearity", "NN")
            source     = "llm" if llm_enabled else "rule"
            src_type   = segments[i].get("segment_type", "unknown")
            dst_type   = segments[i + 1].get("segment_type", "unknown")
            rows.append((
                str(uuid.uuid4()),
                rel_type,
                nuclearity,
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
                        (nn_relation_id, relation_type, nuclearity, src_edu_id, dst_edu_id,
                         meta_context, relation_source)
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s)
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