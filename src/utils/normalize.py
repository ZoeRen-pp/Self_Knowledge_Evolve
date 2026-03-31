"""Term normalization for ontology candidate deduplication."""

import re


def normalize_term(term: str) -> str:
    """Collapse surface forms to a canonical key for candidate grouping.

    Rules:
    - lowercase
    - strip hyphens and spaces
    - collapse 'v' before digits (BGPv4 → bgp4)

    Examples:
        BGP-4       → bgp4
        BGP v4      → bgp4
        BGPv4       → bgp4
        MPLS-TE     → mplste
        IS-IS       → isis
    """
    t = term.lower().strip()
    t = t.replace("-", "").replace(" ", "")
    t = re.sub(r"v(\d)", r"\1", t)
    return t