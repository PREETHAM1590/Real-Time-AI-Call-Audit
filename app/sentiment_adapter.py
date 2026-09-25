"""Local-only sentiment inference adapter, combined with the pure decision boundary.

Scope of this module (see docs/superpowers/plans/2026-09-23-call-audit.md Task 8 step 4
and docs/open-source-models.md's "Sentiment" row): a replaceable adapter that runs a
pinned local three-class checkpoint on already-redacted final CUSTOMER utterance text
and turns its output into the typed `SentimentSignal` shape that `app.sentiment`'s pure,
already-tested decision logic (`sentiment_alert_times`) consumes.

Not in scope here, and not claimed as done: persisting sentiment on a call record, a
worker pipeline stage, an API field, UI display, or model calibration/evaluation on
contact-centre data. `docs/open-source-models.md` is explicit that "a Twitter-trained
checkpoint is not assumed to generalise to calls" and that confidence stays untrusted
until measured against adjudicated data — nothing in this module changes that. This
adapter is unevaluated until that measurement exists; treat its output as provisional
input to `sentiment_alert_times`, never as a release-qualified signal.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Sequence
from typing import Any

from app.artifacts import verified_model_directory
from app.contracts import Utterance
from app.sentiment import SentimentSignal, sentiment_alert_times

MAX_UTTERANCES_PER_CALL = 2_000
MAX_UTTERANCE_CHARS = 4_000
_POSITIVE_LABELS = {"positive", "pos", "label_2"}
_NEGATIVE_LABELS = {"negative", "neg", "label_0"}


class SentimentUnavailable(RuntimeError):
    """The local sentiment model is not configured or failed; callers show unknown, never crash."""


class SentimentValidationError(ValueError):
    """The local model returned a shape this adapter will not trust."""


def _validated_class_probabilities(raw: Any) -> dict[str, float]:
    if not isinstance(raw, list) or not raw:
        raise SentimentValidationError("sentiment model output must be a non-empty list of class scores")
    probabilities: dict[str, float] = {}
    total = 0.0
    for item in raw:
        if not isinstance(item, dict):
            raise SentimentValidationError("each sentiment class score must be an object")
        label, score = item.get("label"), item.get("score")
        if not isinstance(label, str) or not label:
            raise SentimentValidationError("sentiment class label must be a non-empty string")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1:
            raise SentimentValidationError("sentiment class score must be a finite probability")
        normalized_label = label.strip().lower()
        if normalized_label in probabilities:
            raise SentimentValidationError("duplicate sentiment class label")
        probabilities[normalized_label] = float(score)
        total += float(score)
    if not math.isclose(total, 1.0, abs_tol=0.02):
        raise SentimentValidationError("sentiment class scores must sum to approximately 1")
    return probabilities


def _signal_from_class_probabilities(probabilities: dict[str, float]) -> tuple[float, float]:
    positive = sum(value for label, value in probabilities.items() if label in _POSITIVE_LABELS)
    negative = sum(value for label, value in probabilities.items() if label in _NEGATIVE_LABELS)
    signed_score = max(-1.0, min(1.0, positive - negative))
    top_class_probability = max(probabilities.values())
    return signed_score, top_class_probability


class LocalSentimentAdapter:
    """A local, pinned three-class text-classification checkpoint run in-process.

    Deployment provides `artifact_path`/`artifact_sha256` for a verified local model
    directory (no Hub downloads at runtime, matching every other local adapter in this
    codebase) and a `classify` callable bound to that verified artifact — typically a
    `transformers` `pipeline("text-classification", ..., top_k=None)` loaded from the
    verified directory. This class takes the callable rather than constructing it so
    tests can supply a deterministic fake and production wiring can supply the real
    pipeline without this module depending on `transformers` directly.
    """

    def __init__(self, *, artifact_path: str, artifact_sha256: str, classify: Callable[[str], list[dict]], adapter_version: str = "local-sentiment-v1"):
        try:
            self.model_directory = verified_model_directory(artifact_path, artifact_sha256)
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError("local sentiment model artifact path and checksum must verify") from error
        self._classify = classify
        self.artifact_version = f"sha256:{artifact_sha256.lower()}"
        self.adapter_version = adapter_version
        self._warmed = False

    @classmethod
    def from_environment(cls, *, classify: Callable[[str], list[dict]]) -> "LocalSentimentAdapter | None":
        """Return None (not raise) when unconfigured: absence must not block deterministic policy checks."""
        artifact_path, artifact_sha256 = os.environ.get("SENTIMENT_MODEL_PATH"), os.environ.get("SENTIMENT_MODEL_SHA256")
        if not artifact_path or not artifact_sha256:
            return None
        return cls(
            artifact_path=artifact_path, artifact_sha256=artifact_sha256, classify=classify,
            adapter_version=os.environ.get("SENTIMENT_ADAPTER_VERSION", "local-sentiment-v1"),
        )

    def warm(self) -> None:
        """Run one bounded inference so the first real request is not the cold-start request."""
        if self._warmed:
            return
        self.classify_text("warmup")
        self._warmed = True

    def classify_text(self, text: str) -> tuple[float, float]:
        if not isinstance(text, str) or not text.strip():
            raise SentimentValidationError("sentiment input text must be non-empty")
        if len(text) > MAX_UTTERANCE_CHARS:
            raise SentimentValidationError("sentiment input exceeds the bounded character limit")
        try:
            raw = self._classify(text)
        except (SentimentValidationError, SentimentUnavailable):
            raise
        except Exception as error:
            raise SentimentUnavailable("local sentiment inference failed") from error
        return _signal_from_class_probabilities(_validated_class_probabilities(raw))


def compute_call_sentiment(utterances: Sequence[Utterance], adapter: LocalSentimentAdapter | None) -> dict:
    """Score only final redacted CUSTOMER utterances and return alert offsets.

    Never sends AGENT/UNKNOWN/IVR text or non-final (partial) text to the model. Returns
    an explicit unknown/unavailable result rather than raising when no adapter is
    configured or inference fails for a given utterance, so an unavailable sentiment
    model never blocks deterministic policy checks (per AGENTS.md and plan Task 8 step 4).
    """
    if adapter is None:
        return {"status": "UNAVAILABLE", "reason": "no local sentiment model configured", "signals": [], "alert_offsets_ms": []}

    eligible = [utterance for utterance in utterances if utterance.role == "CUSTOMER" and utterance.is_final][:MAX_UTTERANCES_PER_CALL]
    signals: list[SentimentSignal] = []
    failures = 0
    for utterance in eligible:
        try:
            signed_score, top_class_probability = adapter.classify_text(utterance.text_redacted)
        except (SentimentValidationError, SentimentUnavailable):
            failures += 1
            continue
        signals.append({
            "id": utterance.id, "role": utterance.role, "start_ms": utterance.start_ms, "end_ms": utterance.end_ms,
            "is_final": utterance.is_final, "signed_score": signed_score, "top_class_probability": top_class_probability,
        })

    if not signals:
        return {
            "status": "UNAVAILABLE" if failures else "UNKNOWN",
            "reason": "all eligible utterances failed local inference" if failures else "no eligible final CUSTOMER utterances",
            "signals": [], "alert_offsets_ms": [],
        }

    return {
        "status": "OK", "model_artifact": adapter.artifact_version, "adapter_version": adapter.adapter_version,
        "signals": signals, "alert_offsets_ms": sentiment_alert_times(signals),
        "failed_utterance_count": failures,
    }
