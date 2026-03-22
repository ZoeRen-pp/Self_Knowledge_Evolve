"""Content extraction from raw HTML. Primary: trafilatura; fallback: readability-lxml."""

from __future__ import annotations

import re
import logging

log = logging.getLogger(__name__)

_FAQ_PATTERN = re.compile(r'\bQ\s*[:：]|\bA\s*[:：]|frequently asked|常见问题', re.I)
_CODE_BLOCK = re.compile(r'```[\s\S]*?```|^\s{4,}\S', re.M)


class ContentExtractor:
    def extract(self, html: str, url: str) -> dict:
        """Extract clean body text from raw HTML."""
        text, quality = self._try_trafilatura(html, url)
        if not text or quality < 0.3:
            text, quality = self._try_readability(html)
        if not text:
            text = self._fallback_strip_tags(html)
            quality = 0.1

        from src.utils.text import token_count
        tc = token_count(text)
        return {
            "title":          self._extract_title(html),
            "text":           text.strip(),
            "language":       self._detect_language(text),
            "quality":        quality,
            "is_low_quality": tc < 200,
            "token_count":    tc,
        }

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