"""Typed local semantic signals plus a deterministic disposition resolver."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import math
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat, ValidationError

from app.disposition_config import CompiledDispositionConfig


class NoulSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["noul"]
    p_yes: FiniteFloat = Field(ge=0, le=1)
    confidence: FiniteFloat = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=64)


class ChoiceSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["choice"]
    value: str = Field(min_length=1, max_length=128)
    probabilities: dict[str, FiniteFloat]
    confidence: FiniteFloat = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=64)


class ScoreSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["score"]
    value: FiniteFloat = Field(ge=0, le=1)
    confidence: FiniteFloat = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(min_length=1, max_length=64)


Signal = NoulSignal | ChoiceSignal | ScoreSignal


class LocalTypedDecisionAdapter(Protocol):
    """Deployment-injected self-hosted model adapter; no provider in tenant JSON."""

    artifact_version: str
    adapter_version: str

    def evaluate(self, questions: Mapping[str, Any], turns: Sequence[dict[str, Any]]) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DispositionResult:
    status: Literal["RESOLVED", "NEEDS_REVIEW"]
    code: str | None
    parent_code: str | None
    confidence: float | None
    requires_review: bool
    review_reason: str | None
    matched_rule_id: str | None
    signals: dict[str, dict[str, Any]]
    processing_path: Literal["AUTHORITATIVE_FRONT_GATE", "SINGLE_PASS", "TURN_AWARE_MAP_AGGREGATE", "ABSTAIN"]
    config_id: str
    config_version: int
    config_hash: str
    schema_version: str
    model_artifact: str
    adapter_version: str
    usage: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _predicate(node: dict[str, Any], signals: Mapping[str, Signal]) -> bool | None:
    if "all" in node:
        values = [_condition(item, signals) for item in node["all"]]
        if False in values: return False
        return None if None in values else True
    if "any" in node:
        values = [_condition(item, signals) for item in node["any"]]
        if True in values: return True
        return None if None in values else False
    if "not" in node:
        value = _condition(node["not"], signals)
        return None if value is None else not value
    return _condition(node, signals)


def _condition(node: dict[str, Any], signals: Mapping[str, Signal]) -> bool | None:
    sig = signals.get(node.get("signal"))
    if sig is None: return None
    op, value = node.get("operator"), node.get("value")
    if op == "EXISTS": return True
    if isinstance(sig, NoulSignal):
        if op == "NOUL_GTE": return sig.p_yes >= float(value)
        if op == "NOUL_LTE": return sig.p_yes <= float(value)
    if isinstance(sig, ChoiceSignal):
        if op == "CHOICE_EQ": return sig.value == value
        if op == "CHOICE_IN": return sig.value in value if isinstance(value, list) else None
        if op == "CHOICE_CONFIDENCE_GTE": return sig.confidence >= float(value)
    if isinstance(sig, ScoreSignal):
        if op == "SCORE_GTE": return sig.value >= float(value)
        if op == "SCORE_LTE": return sig.value <= float(value)
    return None


def resolve_disposition(config: CompiledDispositionConfig, signals: Mapping[str, Signal], authoritative_facts: Mapping[str, Any]) -> DispositionResult:
    """Apply trusted facts first, then deterministic priority rules."""
    model = "none"
    for gate in config.front_gates:
        if not gate.get("enabled", True): continue
        actual = authoritative_facts.get(gate["source"])
        cond = gate["condition"]
        op, expected = cond["operator"], cond["value"]
        # Missing facts are never negative evidence. Only an explicit EXISTS gate
        # may branch on absence, and current schema uses it for present values.
        matched = actual is not None and ((op == "EQ" and actual == expected) or (op == "NE" and actual != expected)
                   or (op == "IN" and actual in expected) or (op == "NOT_IN" and actual not in expected)
                   or (op == "EXISTS"))
        if matched:
            code = gate["emit"]
            return DispositionResult("RESOLVED", code, config.taxonomy[code].get("parent_code"), 1.0, False, None, gate["id"], {}, "AUTHORITATIVE_FRONT_GATE", config.config_id, config.version, config.content_hash, config.schema_version, model, "none", {"chunks": 0})
    if set(signals) != set(config.questions):
        return _review_result(config, signals, "MISSING_OR_EXTRA_SIGNAL", "ABSTAIN", model, "none", 0)
    for qid, signal in signals.items():
        if signal.type != config.questions[qid].get("type"):
            return _review_result(config, signals, "SIGNAL_TYPE_MISMATCH", "ABSTAIN", model, "none", 0)
    maybe_rule = False
    for rule in config.rules:
        value = _predicate(rule["when"], signals)
        if value is None:
            maybe_rule = True
            continue
        if value:
            code = rule["emit"]
            score = _rule_confidence(rule["when"], signals)
            threshold = config.per_code_threshold.get(code, config.commit_threshold)
            if score is None or score < threshold or score < config.review_threshold:
                return _review_result(config, signals, "LOW_CONFIDENCE", "ABSTAIN", model, "none", 0)
            return DispositionResult("RESOLVED", code, config.taxonomy[code].get("parent_code"), score, False, None, rule["id"], _serialize_signals(signals), "SINGLE_PASS", config.config_id, config.version, config.content_hash, config.schema_version, model, "none", {"chunks": 0})
    if maybe_rule:
        return _review_result(config, signals, "UNCERTAIN_RULE_EVIDENCE", "ABSTAIN", model, "none", 0)
    if config.default_emit not in config.taxonomy or config.default_emit == "REVIEW":
        return _review_result(config, signals, "NO_RULE_MATCHED", "ABSTAIN", model, "none", 0)
    score = min((sig.confidence for sig in signals.values()), default=0.0)
    if score < config.commit_threshold:
        return _review_result(config, signals, "LOW_CONFIDENCE", "ABSTAIN", model, "none", 0)
    code = config.default_emit
    return DispositionResult("RESOLVED", code, config.taxonomy[code].get("parent_code"), score, False, None, None, _serialize_signals(signals), "SINGLE_PASS", config.config_id, config.version, config.content_hash, config.schema_version, model, "none", {"chunks": 0})


def _rule_confidence(node: dict[str, Any], signals: Mapping[str, Signal]) -> float | None:
    if "all" in node or "any" in node:
        children = node.get("all", node.get("any", []))
        vals = [_rule_confidence(child, signals) for child in children]
        vals = [v for v in vals if v is not None]
        return min(vals) if vals else None
    if "not" in node: return _rule_confidence(node["not"], signals)
    sig = signals.get(node.get("signal"))
    return sig.confidence if sig else None


def _serialize_signals(signals: Mapping[str, Signal]) -> dict[str, dict[str, Any]]:
    return {key: value.model_dump(mode="json") for key, value in sorted(signals.items())}


def _review_result(config, signals, reason, path, artifact, adapter, chunks):
    return DispositionResult("NEEDS_REVIEW", None, None, None, True, reason, None, _serialize_signals(signals), path, config.config_id, config.version, config.content_hash, config.schema_version, artifact, adapter, {"chunks": chunks})


def _validated_signals(raw: Mapping[str, Any], config: CompiledDispositionConfig, allowed_evidence: set[str]) -> dict[str, Signal]:
    if not isinstance(raw, Mapping) or set(raw) != set(config.questions):
        raise ValueError("model signal set mismatch")
    parsed: dict[str, Signal] = {}
    types = {"noul": NoulSignal, "choice": ChoiceSignal, "score": ScoreSignal}
    for qid, value in raw.items():
        signal = types[config.questions[qid]["type"]].model_validate(value)
        if any(ref not in allowed_evidence for ref in signal.evidence_ids): raise ValueError("invalid evidence reference")
        if isinstance(signal, ChoiceSignal):
            options = set(config.questions[qid]["criteria"])
            if signal.value not in options or set(signal.probabilities) != options or any(not math.isfinite(float(p)) or p < 0 or p > 1 for p in signal.probabilities.values()):
                raise ValueError("invalid choice values")
            if abs(sum(signal.probabilities.values()) - 1.0) > 0.02: raise ValueError("choice probabilities must sum to one")
            selected_probability = signal.probabilities[signal.value]
            if selected_probability + 0.02 < max(signal.probabilities.values()) or abs(selected_probability - signal.confidence) > 0.02:
                raise ValueError("choice value and confidence must agree with probabilities")
        parsed[qid] = signal
    return parsed


def _group_speaker_turns(utterances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group adjacent redacted segments from the same known speaker; never split that turn."""
    turns: list[dict[str, Any]] = []
    for item in utterances:
        speaker = item.get("speaker_id", item["role"])
        if turns and turns[-1]["role"] == item["role"] and turns[-1]["speaker_id"] == speaker:
            turns[-1]["utterance_ids"].append(str(item["id"]))
            turns[-1]["end_ms"] = max(turns[-1]["end_ms"], item["end_ms"])
            turns[-1]["text_redacted"] += "\n" + item["text_redacted"]
        else:
            turns.append({"id": str(item["id"]), "utterance_ids": [str(item["id"])], "speaker_id": speaker, "role": item["role"], "start_ms": item["start_ms"], "end_ms": item["end_ms"], "text_redacted": item["text_redacted"]})
    return turns


def _windows(turns: list[dict[str, Any]], context_limit: int, window_limit: int, overlap: int, max_chunks: int) -> list[list[dict[str, Any]]] | None:
    """Bound full call and per-request characters; this is not a tokenizer budget."""
    total_chars = sum(len(str(t["text_redacted"])) for t in turns)
    if total_chars > context_limit: return None
    if total_chars <= window_limit: return [turns]
    windows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    chars = 0
    for turn in turns:
        size = len(turn["text_redacted"])
        if size > window_limit: return None
        if current and chars + size > window_limit:
            windows.append(current)
            if len(windows) >= max_chunks: return None
            current = current[-overlap:] if overlap else []
            chars = sum(len(t["text_redacted"]) for t in current)
        current.append(turn); chars += size
    if current: windows.append(current)
    if len(windows) > max_chunks: return None
    return windows


def classify_disposition(transcript: list[dict[str, Any]], facts: Mapping[str, Any], adapter: LocalTypedDecisionAdapter, config: CompiledDispositionConfig) -> DispositionResult:
    """Classify only a final redacted transcript; any uncertainty abstains."""
    front = resolve_disposition(config, {}, facts)
    if front.processing_path == "AUTHORITATIVE_FRONT_GATE": return front
    artifact = str(getattr(adapter, "artifact_version", "unknown"))
    adapter_version = str(getattr(adapter, "adapter_version", "unknown"))
    if not transcript or any(not item.get("is_final", True) for item in transcript):
        return _review_result(config, {}, "INCOMPLETE_TRANSCRIPT", "ABSTAIN", artifact, adapter_version, 0)
    if any(item.get("role") not in {"AGENT", "CUSTOMER"} for item in transcript):
        return _review_result(config, {}, "UNCERTAIN_SPEAKER_ROLE", "ABSTAIN", artifact, adapter_version, 0)
    utterances = sorted((dict(item) for item in transcript), key=lambda item: (item["start_ms"], item["end_ms"], item["id"]))
    if any(not isinstance(t.get("text_redacted"), str) or not t["text_redacted"].strip() for t in utterances):
        return _review_result(config, {}, "EMPTY_TRANSCRIPT_TURN", "ABSTAIN", artifact, adapter_version, 0)
    turns = _group_speaker_turns(utterances)
    windows = _windows(turns, config.context_limit_chars, config.window_chars, config.overlap_turns, config.max_chunks)
    if windows is None:
        return _review_result(config, {}, "CONTEXT_LIMIT", "ABSTAIN", artifact, adapter_version, 0)
    output: list[dict[str, Signal]] = []
    all_ids = {str(t["id"]) for t in utterances}
    try:
        for window in windows:
            raw = adapter.evaluate(config.questions, window)
            output.append(_validated_signals(raw, config, {ref for turn in window for ref in turn["utterance_ids"]} & all_ids))
    except (ValidationError, ValueError, TypeError, KeyError):
        return _review_result(config, {}, "INVALID_MODEL_OUTPUT", "ABSTAIN", artifact, adapter_version, len(windows))
    signals = output[0]
    if len(output) > 1:
        signals = _aggregate(output, config)
        if signals is None:
            return _review_result(config, {}, "CONFLICTING_CHUNK_SIGNALS", "ABSTAIN", artifact, adapter_version, len(windows))
    result = resolve_disposition(config, signals, {})
    path = "TURN_AWARE_MAP_AGGREGATE" if len(windows) > 1 else "SINGLE_PASS"
    return DispositionResult(result.status, result.code, result.parent_code, result.confidence, result.requires_review, result.review_reason, result.matched_rule_id, result.signals, path if result.status == "RESOLVED" else result.processing_path, config.config_id, config.version, config.content_hash, config.schema_version, artifact, adapter_version, {"chunks": len(windows), "input_characters": sum(len(t["text_redacted"]) for t in turns), "speaker_turns": len(turns)})


def _aggregate(chunks: list[dict[str, Signal]], config: CompiledDispositionConfig) -> dict[str, Signal] | None:
    combined: dict[str, Signal] = {}
    for qid, definition in config.questions.items():
        vals = [chunk[qid] for chunk in chunks]
        evidence = sorted({ref for value in vals for ref in value.evidence_ids})
        confidences = [value.confidence for value in vals]
        if isinstance(vals[0], NoulSignal):
            probs = [value.p_yes for value in vals if isinstance(value, NoulSignal)]
            if max(probs) - min(probs) > 0.25: return None
            combined[qid] = NoulSignal(type="noul", p_yes=sum(probs) / len(probs), confidence=min(confidences), evidence_ids=evidence)
        elif isinstance(vals[0], ChoiceSignal):
            choice_vals = [value for value in vals if isinstance(value, ChoiceSignal)]
            top_codes = {value.value for value in choice_vals if value.confidence >= config.review_threshold}
            if len(top_codes) > 1: return None
            options = set(definition["criteria"])
            probs = {option: sum(value.probabilities.get(option, 0.0) for value in choice_vals) / len(choice_vals) for option in options}
            chosen = max(sorted(probs), key=lambda code: probs[code])
            combined[qid] = ChoiceSignal(type="choice", value=chosen, probabilities=probs, confidence=min(confidences), evidence_ids=evidence)
        else:
            score_vals = [value.value for value in vals if isinstance(value, ScoreSignal)]
            if max(score_vals) - min(score_vals) > 0.25: return None
            combined[qid] = ScoreSignal(type="score", value=sum(score_vals) / len(score_vals), confidence=min(confidences), evidence_ids=evidence)
    return combined
