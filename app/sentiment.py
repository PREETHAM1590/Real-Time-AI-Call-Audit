"""Small provider-independent predicates for final sentiment signals."""

from __future__ import annotations

import math
from typing import Any

MIN_WINDOW_SAMPLES = 3
SENTIMENT_DROP_THRESHOLD = 0.4
MIN_SIGNED_SENTIMENT = -1.0
MAX_SIGNED_SENTIMENT = 1.0


def _validated_window(scores: Any, name: str) -> list[float]:
    if not isinstance(scores, list):
        raise ValueError(f"{name} sentiment window must be a list")
    normalized: list[float] = []
    for score in scores:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise ValueError("sentiment scores must be finite numbers from -1 through 1")
        try:
            value = float(score)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("sentiment scores must be finite numbers from -1 through 1") from None
        if not math.isfinite(value) or not MIN_SIGNED_SENTIMENT <= value <= MAX_SIGNED_SENTIMENT:
            raise ValueError("sentiment scores must be finite numbers from -1 through 1")
        normalized.append(value)
    return normalized


def sentiment_drop(previous: list[float], current: list[float]) -> bool:
    """Return whether an eligible adjacent window drops by at least 0.4.

    Scores are signed `p_positive - p_negative` values on [-1, 1]. Callers
    remain responsible for selecting final customer utterances, adjacent
    30-second windows and the separate confidence/cooldown rules.
    """
    prior = _validated_window(previous, "previous")
    recent = _validated_window(current, "current")
    if len(prior) < MIN_WINDOW_SAMPLES or len(recent) < MIN_WINDOW_SAMPLES:
        return False
    prior_mean = math.fsum(prior) / len(prior)
    recent_mean = math.fsum(recent) / len(recent)
    return prior_mean - recent_mean >= SENTIMENT_DROP_THRESHOLD
