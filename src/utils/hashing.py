"""Hashing utilities for deduplication (content hash + SimHash)."""

import hashlib
import re


def content_hash(text: str) -> str:
    """SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _tokenize(text: str) -> list[str]:
    """Tokenize for SimHash: CJK char-level + Latin word-level."""
    tokens: list[str] = []
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff' or '\u3400' <= ch <= '\u4dbf':
            tokens.append(ch)
    words = re.findall(r'[a-zA-Z0-9]+', text)
    tokens.extend(w.lower() for w in words if len(w) > 1)
    return tokens


def simhash(text: str, hashbits: int = 64) -> int:
    """Compute 64-bit SimHash fingerprint of text."""
    tokens = _tokenize(text)
    if not tokens:
        return 0

    v = [0] * hashbits
    for token in tokens:
        h = int(hashlib.md5(token.encode('utf-8')).hexdigest(), 16)
        for i in range(hashbits):
            if (h >> i) & 1:
                v[i] += 1
            else:
                v[i] -= 1

    result = 0
    for i in range(hashbits):
        if v[i] > 0:
            result |= (1 << i)
    return result


def hamming_distance(a: int, b: int) -> int:
    """Number of differing bits between two integers."""
    return bin(a ^ b).count('1')


def jaccard_similarity(text_a: str, text_b: str) -> float:
    """Token-level Jaccard similarity between two texts."""
    set_a = set(_tokenize(text_a))
    set_b = set(_tokenize(text_b))
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)