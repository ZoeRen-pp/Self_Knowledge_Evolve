"""Confidence scoring for facts and segments."""

SOURCE_AUTHORITY: dict[str, float] = {
    'S': 1.00,
    'A': 0.85,
    'B': 0.65,
    'C': 0.40,
}

EXTRACTION_METHOD: dict[str, float] = {
    'manual': 1.00,
    'rule':   0.85,
    'llm':    0.70,
}

# Weights from design doc section 16.2
_W1, _W2, _W3, _W4, _W5 = 0.30, 0.20, 0.20, 0.20, 0.10


def score_fact(
    source_rank: str,
    extraction_method: str,
    ontology_fit: float = 0.8,
    cross_source_consistency: float = 0.5,
    temporal_validity: float = 1.0,
) -> float:
    """
    Weighted confidence score for a fact.
    All inputs except source_rank and extraction_method are 0~1 floats.
    """
    sa = SOURCE_AUTHORITY.get(source_rank, 0.40)
    em = EXTRACTION_METHOD.get(extraction_method, 0.70)
    score = (
        _W1 * sa +
        _W2 * em +
        _W3 * ontology_fit +
        _W4 * cross_source_consistency +
        _W5 * temporal_validity
    )
    return round(max(0.0, min(1.0, score)), 4)


def score_segment(source_rank: str, extraction_quality: float = 0.8) -> float:
    """Simplified segment confidence: 60% source authority + 40% extraction quality."""
    sa = SOURCE_AUTHORITY.get(source_rank, 0.40)
    score = 0.60 * sa + 0.40 * extraction_quality
    return round(max(0.0, min(1.0, score)), 4)


def temporal_validity_score(publish_time_days_ago: int) -> float:
    """Decay temporal validity: docs older than 5 years get lower scores."""
    if publish_time_days_ago <= 365:
        return 1.0
    elif publish_time_days_ago <= 365 * 3:
        return 0.85
    elif publish_time_days_ago <= 365 * 5:
        return 0.70
    else:
        return 0.50
