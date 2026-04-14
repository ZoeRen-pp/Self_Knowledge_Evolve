"""Content extraction from raw HTML. Primary: trafilatura; fallback: readability-lxml."""

from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)

_FAQ_PATTERN = re.compile(r'\bQ\s*[:：]|\bA\s*[:：]|frequently asked|常见问题', re.I)
_CODE_BLOCK = re.compile(r'```[\s\S]*?```|^\s{4,}\S', re.M)

# ── Site-agnostic content quality signals ───────────────────────────
# These catch pages that pass the token-count gate but aren't substantive
# content: author/bio pages, RFC index tables, tag clouds, link farms,
# TOC dumps, bibtex metadata, profile pages. No per-site rules — they
# rest on one language-neutral property: real prose contains sentences.

# Sentence terminal: western or CJK sentence punctuation followed by
# whitespace, closing punctuation, or end-of-text. The trailing context
# is what distinguishes real sentence endings from dots inside URLs,
# IPs, version numbers, and decimal literals.
_SENTENCE_TERMINAL_RE = re.compile(r'[.!?。！？]+(?:\s|["\')\]]|$)')

# Line structure helpers (secondary signals). Bullets and pipe-tables
# are universal typographic conventions, not site-specific.
_LISTY_BULLET_RE = re.compile(r'^\s*[\-\*•◦·▪▫]\s')
_DOT_LEADER_RE = re.compile(r'\.\s*\.\s*\.')


def _is_listy_line(line: str) -> bool:
    """A line looks like list/table/TOC structure, not prose."""
    if line.count('|') >= 2:
        return True
    if _LISTY_BULLET_RE.match(line):
        return True
    if _DOT_LEADER_RE.search(line):
        return True
    return False


def _compute_quality_signals(text: str) -> dict:
    """Return site-agnostic structural metrics for a cleaned text body.

    The primary signal is `sentence_density` — sentence terminals per
    1000 characters. Real prose runs 4–12; profile pages / tag clouds
    / bibtex dumps run near zero; tables and lists run near zero.
    This is language-neutral (western + CJK punctuation both counted)
    and format-neutral (works whether extraction produced one-line
    paragraphs or wrapped columns).
    """
    n_chars = len(text)
    if n_chars == 0:
        return {
            "line_count":       0,
            "char_count":       0,
            "sentence_density": 0.0,
            "listy_ratio":      0.0,
        }
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    n_lines = max(len(lines), 1)
    sentences = len(_SENTENCE_TERMINAL_RE.findall(text))
    listy = sum(1 for l in lines if _is_listy_line(l))
    return {
        "line_count":       n_lines,
        "char_count":       n_chars,
        "sentence_density": round(sentences / (n_chars / 1000), 2),
        "listy_ratio":      round(listy / n_lines, 2),
    }


def _judge_quality(text: str, tc: int) -> tuple[bool, str, dict]:
    """Decide whether a document is structurally substantive.

    Returns (is_low_quality, reason, signals). Rules, in order:
      1. `too_short` — below the minimum token budget for a real article.
      2. `no_sentences` — near-zero sentence density (profile pages,
         RFC index tables, tag clouds, bibtex dumps, link farms). Real
         prose has 4+ sentence terminals per 1000 characters; these
         hit 0 because they are pure structured data.
      3. `listy_dump` — low sentence density AND dominated by pipe-row
         or bullet structure (tabular catalogs with no narrative).
    All rules are site-agnostic and language-neutral.
    """
    if tc < 200:
        return True, "too_short", {}
    sig = _compute_quality_signals(text)
    sd = sig["sentence_density"]
    if sd < 1.0:
        return True, f"no_sentences(density={sd}/1k)", sig
    if sd < 2.0 and sig["listy_ratio"] > 0.4:
        return True, (
            f"listy_dump(density={sd}/1k,listy={sig['listy_ratio']})"
        ), sig
    return False, "", sig


class ContentExtractor:
    def extract(self, html: str, url: str) -> dict:
        """Extract clean body text from raw HTML or plain text."""
        # Plain-text documents (RFC .txt, etc.) — skip HTML extraction entirely
        if self._is_plaintext(html, url):
            text = html.strip()
            quality = 0.9
            title = self._extract_title_from_text(text)
        else:
            # Strip non-content tag blocks once, before all extraction backends.
            # Title is extracted from original HTML (needs <title>/<h1> tags).
            title = self._extract_title(html)
            html = self._preprocess_html(html)
            text, quality = self._try_trafilatura(html, url)
            if not text or quality < 0.3:
                text, quality = self._try_readability(html)
            if not text:
                text = self._fallback_strip_tags(html)
                quality = 0.1

        from src.utils.text import token_count
        clean = text.strip()
        tc = token_count(clean)
        content_type = "text/plain" if self._is_plaintext(html, url) else "text/html"

        is_low, reason, signals = _judge_quality(clean, tc)
        if is_low:
            log.info(
                "Low-quality content rejected: url=%s tc=%d reason=%s signals=%s",
                url or "(n/a)", tc, reason, signals,
            )

        return {
            "title":          title,
            "text":           clean,
            "language":       self._detect_language(clean),
            "quality":        quality,
            "is_low_quality": is_low,
            "low_quality_reason": reason,
            "quality_signals": signals,
            "token_count":    tc,
            "content_type":   content_type,
        }

    @staticmethod
    def _is_plaintext(content: str, url: str) -> bool:
        """Detect if content is plain text (not HTML)."""
        if url.endswith(".txt"):
            return True
        # No HTML tags in first 500 chars → likely plain text
        sample = content[:500]
        return "<" not in sample or not re.search(r"<\w+[\s>]", sample)

    # Column-aligned metadata (e.g. RFC headers): 4+ consecutive internal spaces
    _COLUMN_ALIGNED_RE = re.compile(r"\S.*\s{4,}.*\S")

    @staticmethod
    def _extract_title_from_text(text: str) -> str:
        """Extract title from plain-text document.

        Skips column-aligned metadata lines (RFC/IETF headers use internal
        multi-space padding for column layout — a real title never has that).
        Also skips lines that are too long to be a title.
        """
        for line in text.split("\n"):
            line = line.strip().lstrip("#").strip()
            if not line or len(line) <= 3:
                continue
            if len(line) > 150:
                continue
            if ContentExtractor._COLUMN_ALIGNED_RE.search(line):
                continue
            return line[:255]
        return ""

    def detect_doc_type(self, url: str, title: str, text: str) -> str:
        """Rule C5: classify document type from URL, title, and content signals."""
        url_lower = url.lower()
        title_lower = (title or "").lower()

        if re.search(r'/rfc/|/rfcs/', url_lower) or re.search(r'\brfc\s*\d+', title_lower):
            return "spec"
        if any(p in url_lower for p in ("/config-guide/", "/configuration/", "/cli-reference/")):
            return "vendor_doc"
        if re.search(r'configuration guide|command reference|cli guide', title_lower):
            return "vendor_doc"
        if "/troubleshoot" in url_lower or "troubleshoot" in title_lower:
            return "vendor_doc"
        if url_lower.endswith(".pdf"):
            return "pdf"

        faq_hits = len(_FAQ_PATTERN.findall(text[:3000]))
        words = len(text.split())
        if words and faq_hits / max(words / 100, 1) > 0.3:
            return "faq"

        code_blocks = len(_CODE_BLOCK.findall(text))
        if code_blocks > 3:
            return "tutorial"

        return "tech_article"

    # ── Private ───────────────────────────────────────────────────

    @staticmethod
    def _preprocess_html(html: str) -> str:
        """Strip non-content tag blocks before text extraction.

        Removes tags whose content is never body text regardless of site:
        style, script, noscript, template. Applied once before all extraction
        backends so every backend sees clean HTML.

        Add new tag names here when new noise categories are encountered —
        this is the single extension point for HTML pre-cleaning.
        """
        for tag in ("style", "script", "noscript", "template"):
            html = re.sub(rf"<{tag}[^>]*>[\s\S]*?</{tag}>", "", html, flags=re.I)
        return html

    def _try_trafilatura(self, html: str, url: str) -> tuple[str, float]:
        try:
            import trafilatura
            result = trafilatura.extract(
                html, url=url, include_comments=False,
                include_tables=True, no_fallback=False,
            )
            return (result or "", 0.85 if result else 0.0)
        except Exception as exc:
            log.debug("trafilatura failed: %s", exc)
            return ("", 0.0)

    def _try_readability(self, html: str) -> tuple[str, float]:
        try:
            from readability import Document
            doc = Document(html)
            content = doc.summary()
            text = re.sub(r'<[^>]+>', ' ', content)
            text = re.sub(r'\s+', ' ', text).strip()
            return (text, 0.65 if text else 0.0)
        except Exception as exc:
            log.debug("readability failed: %s", exc)
            return ("", 0.0)

    def _fallback_strip_tags(self, html: str) -> str:
        text = re.sub(r'<[^>]+>', ' ', html)
        return re.sub(r'\s+', ' ', text).strip()

    def _extract_title(self, html: str) -> str:
        m = re.search(r'<title[^>]*>(.*?)</title>', html, re.I | re.S)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.I | re.S)
        if m:
            return re.sub(r'<[^>]+>', '', m.group(1)).strip()
        return ""

    def _detect_language(self, text: str) -> str:
        cjk = sum(1 for ch in text[:500] if '\u4e00' <= ch <= '\u9fff')
        return "zh" if cjk > 20 else "en"