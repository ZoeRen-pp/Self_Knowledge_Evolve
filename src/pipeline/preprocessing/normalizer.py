"""Document normalization after text extraction."""

from __future__ import annotations

import re

from src.utils.text import normalize_text
from src.utils.hashing import content_hash

# Common boilerplate patterns to strip
_BOILERPLATE = [
    re.compile(r'(cookie|privacy) (policy|notice|consent).*?(\n|$)', re.I),
    re.compile(r'(accept|agree) (cookies|terms).*?(\n|$)', re.I),
    re.compile(r'(share|like|tweet|follow us).*?(\n|$)', re.I),
    re.compile(r'©\s*\d{4}.*?all rights reserved.*?(\n|$)', re.I),
    re.compile(r'\b(navigation|breadcrumb|skip to content)\b.*?(\n|$)', re.I),
]


class DocumentNormalizer:
    def normalize(self, raw_text: str, preserve_newlines: bool = False) -> str:
        """Strip boilerplate, normalize whitespace and punctuation.

        Args:
            preserve_newlines: If True, keep line structure intact (for plain-text
                docs like RFCs where headings depend on line position).
        """
        text = raw_text
        for pattern in _BOILERPLATE:
            text = pattern.sub(' ', text)
        text = self._remove_repeated_blocks(text)
        if preserve_newlines:
            return self._normalize_preserve_lines(text)
        # For HTML-extracted text: preserve paragraph boundaries (\n\n)
        # so Stage 2 can split on semantic breaks instead of fixed windows
        return normalize_text(text, preserve_paragraphs=True)

    @staticmethod
    def _normalize_preserve_lines(text: str) -> str:
        """Normalize without collapsing newlines — for plain-text RFC/spec docs."""
        import unicodedata
        text = unicodedata.normalize('NFKC', text)
        # Normalize each line individually: collapse inline whitespace, strip trailing
        lines = []
        for line in text.split('\n'):
            line = re.sub(r'[ \t]+', ' ', line).rstrip()
            lines.append(line)
        # Remove form-feed page headers (RFC page breaks: \f followed by header lines)
        result = '\n'.join(lines)
        result = re.sub(r'\f[^\n]*\n?', '\n', result)
        return result

    def compute_hashes(self, raw_html: str, clean_text: str) -> tuple[str, str]:
        """Return (content_hash_of_raw, normalized_hash_of_clean)."""
        return content_hash(raw_html), content_hash(clean_text)

    def _remove_repeated_blocks(self, text: str, min_len: int = 40) -> str:
        """Remove paragraph-level repeated blocks (same line appearing 3+ times)."""
        lines = text.split('\n')
        from collections import Counter
        counts = Counter(ln.strip() for ln in lines if len(ln.strip()) >= min_len)
        repeated = {ln for ln, cnt in counts.items() if cnt >= 3}
        filtered = [ln for ln in lines if ln.strip() not in repeated]
        return '\n'.join(filtered)