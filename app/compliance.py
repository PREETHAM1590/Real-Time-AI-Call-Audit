"""Versioned, deterministic post-call policy checks over redacted evidence."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from app.contracts import Utterance


MAX_RULESET_BYTES = 64 * 1024
MAX_RULES = 64
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{1,127}$")
_PHONE_NUMBER = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{7,}\d)(?!\w)")
_ACCOUNT_NUMBER = re.compile(r"(?i)\b(?:account|policy|member|customer)\s*(?:number|no\.?|#)\s*[:#-]?\s*[A-Z0-9-]{5,}\b")
_DEFAULT_PHRASES = ("this call may be recorded",)


class RulesetError(ValueError):
    """Ruleset data is invalid or does not match its pinned digest."""


def _field(item: Utterance | Mapping[str, Any], key: str, default=None):
    return getattr(item, key, item.get(key, default) if isinstance(item, Mapping) else default)


def _normalise_phrase(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _phrase_evidence(
    utterances: Sequence[Utterance | Mapping[str, Any]],
    variants: Sequence[str],
    allowed_ids: set[str] | None = None,
) -> list[str]:
    phrases = [_normalise_phrase(phrase) for phrase in variants if _normalise_phrase(phrase)]
    run: list[Utterance | Mapping[str, Any]] = []
    for utterance in [*utterances, None]:
        utterance_id = str(_field(utterance, "id")) if utterance is not None else None
        if (
            utterance is not None
            and _field(utterance, "is_final", True)
            and _field(utterance, "role") == "AGENT"
            and (allowed_ids is None or utterance_id in allowed_ids)
        ):
            run.append(utterance)
            continue
        text = _normalise_phrase(" ".join(str(_field(item, "text_redacted", "")) for item in run))
        if text:
            for phrase in phrases:
                start = text.find(phrase)
                if start >= 0:
                    # Retain only utterance IDs whose text contributes to the
                    # matched span, while keeping references canonical.
                    ids = []
                    offset = 0
                    end = start + len(phrase)
                    for item in run:
                        item_text = _normalise_phrase(str(_field(item, "text_redacted", "")))
                        item_end = offset + len(item_text)
                        if item_text and item_end > start and offset < end:
                            ids.append(str(_field(item, "id")))
                        offset = item_end + 1
                    return ids
        run = []
    return []


def disclosure_state(
    utterances: list[Utterance],
    opportunity_ms: int,
    ended: bool,
    reliable: bool,
    phrase_variants: Sequence[str] = _DEFAULT_PHRASES,
    required_opportunity_ms: int = 30_000,
    phrase_satisfied: bool | None = None,
) -> str:
    """Classify an already-clipped opening opportunity window.

    `opportunity_ms` is elapsed eligible call time after holds are removed;
    `required_opportunity_ms` is the versioned rule's threshold. The caller
    must supply only final utterances wholly inside the opportunity window.
    """
    if not reliable or any(not _field(u, "is_final", True) or _field(u, "role") == "UNKNOWN" for u in utterances):
        return "UNKNOWN"
    if phrase_satisfied is None:
        phrase_satisfied = bool(_phrase_evidence(utterances, phrase_variants))
    if phrase_satisfied:
        return "SATISFIED"
    if opportunity_ms >= required_opportunity_ms:
        return "POTENTIAL_VIOLATION"
    return "UNKNOWN" if ended else "PENDING"


def compile_ruleset(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the small phrase-policy subset and return canonical provenance."""
    if not isinstance(raw, Mapping):
        raise RulesetError("Ruleset must be an object")
    try:
        encoded = json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise RulesetError("Ruleset must contain bounded JSON data") from None
    if len(encoded) > MAX_RULESET_BYTES:
        raise RulesetError("Ruleset exceeds configured size limit")
    if set(raw) != {"schema_version", "ruleset_id", "version", "policy_text_version", "rules"}:
        raise RulesetError("Ruleset fields are incomplete or unsupported")
    if raw.get("schema_version") != "1.0.0":
        raise RulesetError("Unsupported ruleset schema version")
    ruleset_id, version, policy_version = raw.get("ruleset_id"), raw.get("version"), raw.get("policy_text_version")
    if not isinstance(ruleset_id, str) or not _IDENTIFIER.fullmatch(ruleset_id):
        raise RulesetError("Invalid ruleset identifier")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 2**31 - 1:
        raise RulesetError("Ruleset version must be a positive PostgreSQL integer")
    if not isinstance(policy_version, str) or not _IDENTIFIER.fullmatch(policy_version):
        raise RulesetError("Invalid policy text version")
    rules = raw.get("rules")
    if not isinstance(rules, list) or not rules or len(rules) > MAX_RULES:
        raise RulesetError("Ruleset must contain 1–64 rules")
    seen = set()
    compiled_rules = []
    for rule in rules:
        if not isinstance(rule, Mapping):
            raise RulesetError("Rule must be an object")
        kind = rule.get("type")
        if not isinstance(kind, str):
            raise RulesetError("Rule type must be a string")
        required = {"id", "type", "severity", "remediation", "call_types"}
        optional = set()
        if kind in {"OPENING_DISCLOSURE", "CLOSING_PHRASE"}:
            required.add("phrase_variants")
            optional = {"opportunity_ms", "window_ms"}
        elif kind != "SENSITIVE_NUMBER_ADVISORY":
            raise RulesetError("Unsupported deterministic rule type")
        if set(rule) - required - optional or required - set(rule):
            raise RulesetError("Rule fields are incomplete or unsupported")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not _IDENTIFIER.fullmatch(rule_id) or rule_id in seen:
            raise RulesetError("Rule identifiers must be unique")
        seen.add(rule_id)
        severity = rule.get("severity")
        if not isinstance(severity, str) or severity not in {"LOW", "MEDIUM", "HIGH"}:
            raise RulesetError("Unsupported finding severity")
        call_types = rule.get("call_types")
        if not isinstance(call_types, list) or not call_types or len(call_types) > 64 or any(not isinstance(item, str) or not item or len(item) > 128 for item in call_types):
            raise RulesetError("call_types must be a bounded nonempty string list")
        remediation = rule.get("remediation")
        if not isinstance(remediation, str) or not remediation.strip() or len(remediation) > 500:
            raise RulesetError("Remediation must be bounded safe text")
        if kind in {"OPENING_DISCLOSURE", "CLOSING_PHRASE"}:
            phrases = rule.get("phrase_variants")
            if not isinstance(phrases, list) or not phrases or len(phrases) > 32 or any(not isinstance(p, str) or not p.strip() or len(p) > 200 for p in phrases):
                raise RulesetError("Phrase variants must be bounded nonempty strings")
            duration_key = "opportunity_ms" if kind == "OPENING_DISCLOSURE" else "window_ms"
            duration = rule.get(duration_key)
            if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 10 * 60 * 1000:
                raise RulesetError(f"{duration_key} must be between 1 and 600000 milliseconds")
            if kind == "OPENING_DISCLOSURE" and "window_ms" in rule or kind == "CLOSING_PHRASE" and "opportunity_ms" in rule:
                raise RulesetError("Rule duration does not match rule type")
        compiled_rules.append({**dict(rule), "policy_text_version": policy_version})
    if sum(rule["type"] == "SENSITIVE_NUMBER_ADVISORY" for rule in compiled_rules) != 1:
        raise RulesetError("Ruleset must define exactly one sensitive-number advisory rule")
    normalized = json.loads(json.dumps(dict(raw), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return {
        "ruleset_id": ruleset_id,
        "ruleset_version": str(version),
        "policy_text_version": policy_version,
        "ruleset_hash": hashlib.sha256(json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "rules": compiled_rules,
    }


def load_ruleset(path: str | Path, expected_sha256: str) -> dict[str, Any]:
    """Load a local-only, operator-pinned rules file; no mutable runtime fetch."""
    if not isinstance(expected_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise RulesetError("Ruleset SHA-256 is required")
    candidate = Path(path)
    if not candidate.is_absolute():
        raise RulesetError("Ruleset path must be absolute")
    try:
        payload = candidate.read_bytes()
    except OSError:
        raise RulesetError("Local ruleset is unavailable") from None
    if len(payload) > MAX_RULESET_BYTES or hashlib.sha256(payload).hexdigest() != expected_sha256:
        raise RulesetError("Local ruleset checksum mismatch")
    try:
        raw = json.loads(payload)
    except (ValueError, UnicodeDecodeError):
        raise RulesetError("Local ruleset is invalid JSON") from None
    return compile_ruleset(raw)


def _normalise_holds(value: Any, duration_ms: int) -> list[tuple[int, int]] | None:
    if not isinstance(value, list) or len(value) > 10_000:
        return None
    holds = []
    for item in value:
        tag = item.get("tag") if isinstance(item, Mapping) else None
        if not isinstance(item, Mapping) or not isinstance(tag, str) or tag not in {"HOLD", "IVR"}:
            return None
        start, end = item.get("start_ms"), item.get("end_ms")
        if isinstance(start, bool) or not isinstance(start, int) or isinstance(end, bool) or not isinstance(end, int) or not 0 <= start <= end <= duration_ms:
            return None
        holds.append((start, end))
    merged: list[list[int]] = []
    for start, end in sorted(holds):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _hold_overlap(holds: Sequence[tuple[int, int]], start: int, end: int) -> int:
    return sum(max(0, min(end, hold_end) - max(start, hold_start)) for hold_start, hold_end in holds)


def _opening_deadline(start: int, opportunity_ms: int, holds: Sequence[tuple[int, int]]) -> int:
    deadline = start + opportunity_ms
    # Holds pause the clock. Sorted, merged intervals can each be consumed
    # once: if one starts before the current deadline, shift the deadline by
    # its full portion after `start`. This also brings a later hold into scope
    # without rescanning prior intervals or iterating once per held millisecond.
    for begin, end in holds:
        if begin >= deadline:
            break
        if end > start:
            deadline += end - max(begin, start)
    return deadline


def _new_finding(context: Mapping[str, Any], ruleset: Mapping[str, Any], rule: Mapping[str, Any], status: str, evidence: Sequence[str], deadline_ms: int | None) -> dict[str, Any]:
    evidence_ids = sorted(set(evidence))
    fingerprint = hashlib.sha256(json.dumps(evidence_ids, separators=(",", ":")).encode()).hexdigest()
    return {
        "organisation_id": str(context["organisation_id"]),
        "call_id": str(context["call_id"]),
        "transcript_revision": context["transcript_revision"],
        "rule_id": rule["id"],
        "ruleset_version": ruleset["ruleset_version"],
        "ruleset_hash": ruleset["ruleset_hash"],
        "policy_text_version": rule["policy_text_version"],
        "status": status,
        "severity": rule["severity"],
        "evidence_ids": evidence_ids,
        "evidence_fingerprint": fingerprint,
        "deadline_ms": deadline_ms,
        "remediation": rule["remediation"],
    }


def evaluate_rules(utterances: Sequence[Utterance | Mapping[str, Any]], context: Mapping[str, Any], ruleset: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Evaluate final redacted evidence inside trusted timing/role windows."""
    required_context = {"organisation_id", "call_id", "transcript_revision", "call_type", "agent_connected_ms", "call_duration_ms", "holds", "complete", "timing_reliable"}
    if not required_context <= set(context):
        raise ValueError("Policy context is incomplete")
    compiled = ruleset if "ruleset_hash" in ruleset else compile_ruleset(ruleset)
    duration = context["call_duration_ms"]
    connected = context["agent_connected_ms"]
    revision = context["transcript_revision"]
    identifiers = (context["organisation_id"], context["call_id"], context["call_type"])
    reliable = all(isinstance(v, str) and v and len(v) <= 128 for v in identifiers)
    reliable &= isinstance(revision, int) and not isinstance(revision, bool) and 1 <= revision <= 2**31 - 1
    reliable &= isinstance(duration, int) and not isinstance(duration, bool) and 0 <= duration <= 2**31 - 1
    reliable &= isinstance(connected, int) and not isinstance(connected, bool) and 0 <= connected <= duration if isinstance(duration, int) and not isinstance(duration, bool) else False
    reliable &= context["complete"] is True and context["timing_reliable"] is True
    holds = _normalise_holds(context["holds"], duration) if isinstance(duration, int) and not isinstance(duration, bool) and duration >= 0 else None
    reliable &= holds is not None
    if any(not _field(u, "is_final", True) for u in utterances):
        reliable = False
    safe_holds = holds or []
    findings = []
    for rule in compiled["rules"]:
        if "*" not in rule["call_types"] and context["call_type"] not in rule["call_types"]:
            continue
        if rule["type"] == "OPENING_DISCLOSURE":
            deadline = _opening_deadline(connected, rule["opportunity_ms"], safe_holds) if isinstance(connected, int) and not isinstance(connected, bool) else None
            if deadline is None or not isinstance(duration, int) or not isinstance(connected, int):
                eligible = []
                elapsed = 0
            else:
                eligible = []
                for utterance in utterances:
                    start, end = _field(utterance, "start_ms"), _field(utterance, "end_ms")
                    if isinstance(start, int) and isinstance(end, int) and connected <= start <= end <= min(duration, deadline) and _hold_overlap(safe_holds, start, end) == 0:
                        eligible.append(utterance)
                opportunity_end = min(duration, deadline)
                elapsed = max(0, opportunity_end - connected - _hold_overlap(safe_holds, connected, opportunity_end))
            window_end = min(duration, deadline) if isinstance(duration, int) and deadline is not None else None
            unknown_role = False
            if window_end is not None:
                for utterance in utterances:
                    if _field(utterance, "role") != "UNKNOWN":
                        continue
                    start, end = _field(utterance, "start_ms"), _field(utterance, "end_ms")
                    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
                        continue
                    overlap_start, overlap_end = max(start, connected), min(end, window_end)
                    opportunity_overlap = max(0, overlap_end - overlap_start)
                    if opportunity_overlap and _hold_overlap(safe_holds, overlap_start, overlap_end) < opportunity_overlap:
                        unknown_role = True
                        break
            allowed_ids = {str(_field(u, "id")) for u in eligible}
            phrase_evidence = _phrase_evidence(utterances, rule["phrase_variants"], allowed_ids)
            state = disclosure_state(
                eligible,
                elapsed,
                ended=context.get("complete") is True,
                reliable=bool(reliable and deadline is not None and not unknown_role),
                phrase_variants=rule["phrase_variants"],
                required_opportunity_ms=rule["opportunity_ms"],
                phrase_satisfied=bool(phrase_evidence),
            )
            evidence = phrase_evidence if state == "SATISFIED" else []
            findings.append(_new_finding(context, compiled, rule, state, evidence, deadline))
        elif rule["type"] == "CLOSING_PHRASE":
            if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
                window_start = window_end = None
            else:
                window_end = duration
                window_start = max(0, duration - rule["window_ms"])
            eligible = []
            if window_start is not None and connected is not None:
                for utterance in utterances:
                    start, end = _field(utterance, "start_ms"), _field(utterance, "end_ms")
                    if isinstance(start, int) and isinstance(end, int) and window_start <= start <= end <= window_end and _hold_overlap(safe_holds, start, end) == 0:
                        eligible.append(utterance)
            unknown_role = any(_field(u, "role") == "UNKNOWN" for u in eligible)
            if not reliable or window_start is None or unknown_role:
                state = "UNKNOWN"
            elif _phrase_evidence(
                utterances,
                rule["phrase_variants"],
                {str(_field(u, "id")) for u in eligible},
            ):
                state = "SATISFIED"
            else:
                state = "POTENTIAL_VIOLATION"
            evidence = (
                _phrase_evidence(utterances, rule["phrase_variants"], {str(_field(u, "id")) for u in eligible})
                if state == "SATISFIED"
                else []
            )
            findings.append(_new_finding(context, compiled, rule, state, evidence, window_start))
    return findings


def scan_sensitive_numbers(segments: Sequence[Mapping[str, Any]], ruleset: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Detect likely numbers before redaction and return only safe metadata."""
    if ruleset is None:
        return []
    compiled = ruleset if "ruleset_hash" in ruleset else compile_ruleset(ruleset)
    rule = next((item for item in compiled["rules"] if item["type"] == "SENSITIVE_NUMBER_ADVISORY"), None)
    if rule is None:
        return []
    output = []
    for segment in segments:
        text = segment.get("text")
        segment_id = segment.get("segment_id")
        if not isinstance(text, str) or not isinstance(segment_id, str):
            continue
        if _PHONE_NUMBER.search(text) or _ACCOUNT_NUMBER.search(text):
            evidence = hashlib.sha256(segment_id.encode("utf-8")).hexdigest()[:32]
            output.append({"rule_id": rule["id"], "ruleset_version": compiled["ruleset_version"], "ruleset_hash": compiled["ruleset_hash"], "status": "ADVISORY", "severity": rule["severity"], "policy_text_version": rule["policy_text_version"], "evidence_ids": [evidence], "deadline_ms": segment.get("end_ms"), "remediation": rule["remediation"]})
    return output
