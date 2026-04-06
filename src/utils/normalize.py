"""Term normalization for ontology candidate deduplication.

Produces space-separated lowercase tokens that preserve word boundaries,
enabling downstream token-level operations (Jaccard, containment, display).
"""

import re

# Hyphenated terms where the hyphen is semantically meaningful (abbreviation-abbreviation)
# e.g. IS-IS, MPLS-TE, MP-BGP, OSPF-TE, L2-VPN
_PRESERVE_HYPHEN_RE = re.compile(r"^[A-Z][A-Z0-9]{0,5}-[A-Z][A-Z0-9]{0,5}$")

# Simple English plural suffixes — only applied to non-acronym tokens > 3 chars
_PLURAL_RULES = [
    (re.compile(r"ies$"), "y"),       # policies → policy
    (re.compile(r"ses$"), "s"),       # addresses → address  (but not "ses" → "s")
    (re.compile(r"([^s])s$"), r"\1"), # routers → router, networks → network
]


def normalize_term(term: str) -> str:
    """Collapse surface forms to a canonical key for candidate grouping.

    Preserves word boundaries as spaces for token-level operations.

    Rules:
    - Strip parenthetical content: "xxx (YYY)" → "xxx"
    - Strip leading articles: "the/a/an xxx" → "xxx"
    - Lowercase
    - Hyphens: preserve for abbreviation pairs (IS-IS, MPLS-TE); else replace with space
    - Simple plural stripping on non-acronym tokens > 3 chars
    - Collapse version markers: BGPv4 → bgp 4

    Examples:
        BGP-4                → bgp 4
        BGP v4               → bgp 4
        BGPv4                → bgp 4
        MPLS-TE              → mpls-te
        IS-IS                → is-is
        Router-ID            → router id
        Router IDs           → router id
        OSPF Router-ID       → ospf router id
        the BGP protocol     → bgp protocol
        network layer reachability information (NLRI) → network layer reachability information
        broadcast networks   → broadcast network
    """
    t = term.strip()
    if not t:
        return ""

    # Strip parenthetical content
    t = re.sub(r"\s*\([^)]*\)\s*", " ", t).strip()

    # Strip leading articles
    t = re.sub(r"^(the|a|an)\s+", "", t, flags=re.I)

    # Handle hyphens: preserve abbreviation-abbreviation, else replace with space
    tokens = t.split()
    processed = []
    for token in tokens:
        if _PRESERVE_HYPHEN_RE.match(token):
            # Abbreviation pair: keep hyphen, lowercase
            processed.append(token.lower())
        else:
            # Replace hyphens with space, will be split further
            processed.append(token.replace("-", " ").lower())

    # Re-split (hyphen replacement may have introduced new spaces)
    final_tokens = " ".join(processed).split()

    # Collapse version markers: "v4" → "4", "bgpv4" → "bgp 4"
    expanded = []
    for tok in final_tokens:
        m = re.match(r"^(.+?)v(\d+)$", tok)
        if m and m.group(1):
            expanded.append(m.group(1))
            expanded.append(m.group(2))
        elif re.match(r"^v\d+$", tok):
            expanded.append(tok[1:])  # standalone "v4" → "4"
        else:
            expanded.append(tok)

    # Plural stripping
    # - Non-acronym tokens > 3 chars: apply standard rules
    # - Short tokens ending in 's' where base is all-uppercase (IDs→id, LSAs→lsa): strip 's'
    # - Skip tokens with hyphens, known non-plural words
    _NO_STRIP = {"this", "that", "its", "has", "was", "his", "bus", "plus",
                 "thus", "is", "as", "us", "yes", "dns", "qos"}
    result = []
    for tok in expanded:
        if tok in _NO_STRIP or "-" in tok:
            result.append(tok)
            continue
        if len(tok) > 3 and not tok.isupper():
            for pattern, replacement in _PLURAL_RULES:
                new_tok = pattern.sub(replacement, tok)
                if new_tok != tok:
                    tok = new_tok
                    break
        elif tok.endswith("s") and len(tok) >= 3 and tok[:-1].isalpha():
            # Handle short plural-like tokens: "ids" → "id", "lsas" → "lsa"
            tok = tok[:-1]
        result.append(tok)

    return " ".join(result)


def extract_abbreviation(term: str) -> str | None:
    """Extract abbreviation from parenthetical if present.

    "network layer reachability information (NLRI)" → "NLRI"
    "Border Gateway Protocol (BGP)" → "BGP"
    "simple text" → None
    """
    m = re.search(r"\(([A-Za-z][A-Za-z0-9\-]{0,10})\)\s*$", term)
    return m.group(1) if m else None


def tokenize_normalized(normalized: str) -> set[str]:
    """Split a normalized form into token set for Jaccard/containment operations."""
    return set(normalized.split()) if normalized else set()