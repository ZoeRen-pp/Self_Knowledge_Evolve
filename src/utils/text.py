"""Text normalization utilities used across the pipeline."""

import re
import unicodedata

_CHINESE_TO_ASCII = {
    '，': ',', '。': '.', '！': '!', '？': '?', '；': ';', '：': ':',
    '（': '(', '）': ')', '【': '[', '】': ']', '"': '"', '"': '"',
    '\u2018': "'", '\u2019': "'", '、': ',', '…': '...', '—': '-', '–': '-',
}


def normalize_text(text: str) -> str:
    """Normalize text: full-width→half-width, punctuation, whitespace, lowercase."""
    if not text:
        return ""
    # NFKC: full-width → half-width, composed forms
    text = unicodedata.normalize('NFKC', text)
    # Chinese punctuation → ASCII
    for ch, asc in _CHINESE_TO_ASCII.items():
        text = text.replace(ch, asc)
    # Collapse all whitespace (including \t \r \n) to single space
    text = re.sub(r'\s+', ' ', text)
    text = text.strip().lower()
    return text


def token_count(text: str) -> int:
    """Count tokens: CJK chars count individually; Latin words split by whitespace."""
    if not text:
        return 0
    cjk = sum(
        1 for ch in text
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
    )
    # Remove CJK then count whitespace-delimited words
    non_cjk = re.sub(r'[\u4e00-\u9fff\u3400-\u4dbf]', ' ', text)
    words = len([w for w in non_cjk.split() if w])
    return cjk + words


def truncate(text: str, max_tokens: int) -> str:
    """Truncate text to max_tokens, respecting word boundaries."""
    if token_count(text) <= max_tokens:
        return text
    result: list[str] = []
    count = 0
    for word in text.split():
        cjk_in_word = sum(
            1 for ch in word
            if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf'
        )
        increment = cjk_in_word if cjk_in_word else 1
        if count + increment > max_tokens:
            break
        result.append(word)
        count += increment
    return ' '.join(result)


def sliding_window_split(text: str, window: int = 512, overlap: int = 64) -> list[str]:
    """Split long text into overlapping windows (token-based)."""
    words = text.split()
    if len(words) <= window:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + window, len(words))
        chunks.append(' '.join(words[start:end]))
        if end == len(words):
            break
        start += window - overlap
    return chunks
