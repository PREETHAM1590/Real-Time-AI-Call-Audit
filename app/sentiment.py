"""Small provider-independent predicates for final sentiment signals."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Literal, TypedDict

MIN_WINDOW_SAMPLES = 3
SENTIMENT_DROP_THRESHOLD = 0.4
MIN_SIGNED_SENTIMENT = -1.0
MAX_SIGNED_SENTIMENT = 1.0
WINDOW_MS = 30_000
MIN_TOP_CLASS_PROBABILITY = 0.7
ALERT_COOLDOWN_MS = 90_000


class SentimentSignal(TypedDict):
    """Already-inferred sentiment attached to one transcript utterance."""

    role: Literal["AGENT", "CUSTOMER", "UNKNOWN", "IVR"]
    start_ms: int
    end_ms: int
    is_final: bool
    signed_score: float
    top_class_probability: float


def _validated_probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("top-class probability must be finite and from 0 through 1")
    try:
        probability = float(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("top-class probability must be finite and from 0 through 1") from None
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("top-class probability must be finite and from 0 through 1")
    return probability


def _validated_offset(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer millisecond offset")
    return value


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


def sentiment_alert_times(signals: Sequence[SentimentSignal]) -> list[int]:
    """Return current-window end offsets for qualifying adjacent-window drops.

    Only final CUSTOMER signals participate. Fixed windows use `start_ms`; a
    returned offset is the end of the later window. Model inference and event
    delivery remain the caller's responsibility.
    """
    windows: dict[int, list[tuple[float, float]]] = {}
    for signal in signals:
        role = signal.get("role")
        is_final = signal.get("is_final")
        if role not in {"AGENT", "CUSTOMER", "UNKNOWN", "IVR"} or type(is_final) is not bool:
            raise ValueError("sentiment signal has an invalid role or finality")
        if not is_final or role != "CUSTOMER":
            continue

        start_ms = _validated_offset(signal.get("start_ms"), "start_ms")
        end_ms = _validated_offset(signal.get("end_ms"), "end_ms")
        if end_ms < start_ms:
            raise ValueError("end_ms must not precede start_ms")
        score = _validated_window([signal.get("signed_score")], "signal")[0]
        probability = _validated_probability(signal.get("top_class_probability"))
        windows.setdefault(start_ms // WINDOW_MS, []).append((score, probability))

    alerts: list[int] = []
    last_alert_ms: int | None = None
    for current_index in sorted(windows):
        previous = windows.get(current_index - 1)
        current = windows[current_index]
        if previous is None or not sentiment_drop(
            [score for score, _ in previous], [score for score, _ in current]
        ):
            continue
        probabilities = [probability for _, probability in previous + current]
        if math.fsum(probability - MIN_TOP_CLASS_PROBABILITY for probability in probabilities) < 0:
            continue
        alert_ms = (current_index + 1) * WINDOW_MS
        if last_alert_ms is None or alert_ms - last_alert_ms >= ALERT_COOLDOWN_MS:
            alerts.append(alert_ms)
            last_alert_ms = alert_ms
    return alerts
