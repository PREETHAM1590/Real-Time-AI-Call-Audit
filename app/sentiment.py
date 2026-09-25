"""Small provider-independent predicates for final sentiment signals."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
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

    id: str
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


def _decimal_mean(values: Sequence[float]) -> Decimal:
    return sum((Decimal(str(value)) for value in values), Decimal(0)) / len(values)


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
    return _decimal_mean(prior) - _decimal_mean(recent) >= Decimal(str(SENTIMENT_DROP_THRESHOLD))


def sentiment_alert_times(signals: Sequence[SentimentSignal]) -> list[int]:
    """Return current-window end offsets for qualifying adjacent-window drops.

    Only final CUSTOMER signals participate. Fixed windows use `start_ms`; a
    returned offset is the end of the later window. Duplicate utterance IDs
    are rejected so repeated finals cannot inflate sample counts. Model
    inference and event delivery remain the caller's responsibility.
    """
    windows: dict[int, list[tuple[float, float]]] = {}
    seen_ids: set[str] = set()
    for signal in signals:
        if not isinstance(signal, Mapping):
            raise ValueError("sentiment signals must be mappings")
        signal_id = signal.get("id")
        role = signal.get("role")
        is_final = signal.get("is_final")
        if not isinstance(signal_id, str) or not 1 <= len(signal_id) <= 128:
            raise ValueError("sentiment signal ID must contain 1 through 128 characters")
        if signal_id in seen_ids:
            raise ValueError("duplicate sentiment signal ID")
        seen_ids.add(signal_id)
        if not isinstance(role, str) or role not in {"AGENT", "CUSTOMER", "UNKNOWN", "IVR"} or type(is_final) is not bool:
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
        if _decimal_mean(probabilities) < Decimal(str(MIN_TOP_CLASS_PROBABILITY)):
            continue
        alert_ms = (current_index + 1) * WINDOW_MS
        if last_alert_ms is None or alert_ms - last_alert_ms >= ALERT_COOLDOWN_MS:
            alerts.append(alert_ms)
            last_alert_ms = alert_ms
    return alerts


TREND_WINDOW_MS = 60_000


def customer_speech_trend(signals: Sequence[SentimentSignal]) -> dict | None:
    """Duration-weighted mean signed score of the first vs. last 60s of final CUSTOMER speech.

    Spec section 11: "Aggregate contiguous turns with duration weights; compare the
    first and last 60 seconds of customer speech for the call summary. Missing customer
    speech gives null summary." This implements that first/last-60-seconds comparison,
    weighting each qualifying utterance's contribution by its own duration; it does not
    attempt turn-level grouping, since the spec does not define a turn boundary and
    inventing one here would be unverified behaviour, not a measured requirement. When
    total qualifying speech is under 120s the two windows may share utterances; that
    overlap is expected, not an error.
    """
    eligible = [signal for signal in signals if isinstance(signal, Mapping) and signal.get("role") == "CUSTOMER" and signal.get("is_final") is True]
    ordered = sorted(eligible, key=lambda signal: _validated_offset(signal.get("start_ms"), "start_ms"))
    if not ordered:
        return None

    def _windowed_mean(items: Sequence[SentimentSignal]) -> tuple[Decimal, int]:
        total_duration_ms = 0
        weighted_sum = Decimal(0)
        for signal in items:
            start_ms, end_ms = _validated_offset(signal.get("start_ms"), "start_ms"), _validated_offset(signal.get("end_ms"), "end_ms")
            if end_ms < start_ms:
                raise ValueError("end_ms must not precede start_ms")
            duration_ms = end_ms - start_ms
            score = _validated_window([signal.get("signed_score")], "signal")[0]
            weighted_sum += Decimal(str(score)) * Decimal(duration_ms)
            total_duration_ms += duration_ms
            if total_duration_ms >= TREND_WINDOW_MS:
                break
        if total_duration_ms == 0:
            return Decimal(0), 0
        return weighted_sum / Decimal(total_duration_ms), total_duration_ms

    first_mean, first_duration_ms = _windowed_mean(ordered)
    last_mean, last_duration_ms = _windowed_mean(list(reversed(ordered)))
    if first_duration_ms == 0 or last_duration_ms == 0:
        return None
    return {
        "first_60s_mean_signed_score": float(first_mean),
        "first_60s_customer_speech_ms": first_duration_ms,
        "last_60s_mean_signed_score": float(last_mean),
        "last_60s_customer_speech_ms": last_duration_ms,
        "trend_delta": float(last_mean - first_mean),
    }
