"""Safe, provider-neutral business configuration for disposition resolution."""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any


_ID = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{1,127}$")
_FORBIDDEN_KEYS = {"provider", "model", "adapter", "api_profile", "endpoint", "tenant_id", "organisation_id", "organization_id", "credential", "credentials", "token", "password", "secret", "api_key", "apikey", "authorization", "expression", "script"}
_SIGNAL_TYPES = {"noul", "choice", "score"}
_OPERATORS = {"NOUL_GTE", "NOUL_LTE", "CHOICE_EQ", "CHOICE_IN", "CHOICE_CONFIDENCE_GTE", "SCORE_GTE", "SCORE_LTE", "EXISTS"}
_MAX_CONFIG_BYTES = 256 * 1024
_MAX_CONTAINER_ITEMS = 4096
_MAX_NODES = 20_000
_MAX_STRING_CHARS = 16_384
_MAX_INTEGER = 2**63 - 1
_MIN_INTEGER = -(2**63)


class ConfigError(ValueError):
    def __init__(self, errors: list[dict[str, str]]):
        super().__init__("Invalid disposition configuration")
        self.errors = errors


@dataclass(frozen=True)
class CompiledDispositionConfig:
    config_id: str
    use_case_id: str
    version: int
    display_name: str
    questions: dict[str, dict[str, Any]]
    taxonomy: dict[str, dict[str, Any]]
    front_gates: tuple[dict[str, Any], ...]
    rules: tuple[dict[str, Any], ...]
    default_emit: str
    commit_threshold: float
    review_threshold: float
    per_code_threshold: dict[str, float]
    context_limit_chars: int
    window_chars: int
    overlap_turns: int
    max_chunks: int
    normalized: dict[str, Any]
    content_hash: str
    schema_version: str = "1.0.0"


def _err(path: str, message: str) -> dict[str, str]:
    return {"path": path, "message": message}


def _bounded_number(value: Any, path: str, errors: list[dict[str, str]]) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        errors.append(_err(path, "must be a finite number between 0 and 1"))
        return None
    # Compare integers before converting: math.isfinite(10**10000) raises
    # OverflowError even though it should simply be rejected as out of range.
    if isinstance(value, int):
        if not 0 <= value <= 1:
            errors.append(_err(path, "must be a finite number between 0 and 1"))
            return None
    elif not math.isfinite(value) or not 0 <= value <= 1:
        errors.append(_err(path, "must be a finite number between 0 and 1"))
        return None
    return float(value)


def compile_disposition_config(raw: dict) -> CompiledDispositionConfig:
    """Validate supported JSON and precompute a deterministic immutable form.

    Only the small declarative subset used by the current resolver is accepted.
    In particular, tenant identity and model/provider selection are never config
    fields: organisation scope comes from the authenticated request/job.
    """
    errors: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        raise ConfigError([_err("$", "must be an object")])

    try:
        encoded_raw = json.dumps(raw, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ConfigError([_err("$", "must be bounded JSON data")]) from None
    if len(encoded_raw) > _MAX_CONFIG_BYTES:
        raise ConfigError([_err("$", f"configuration exceeds {_MAX_CONFIG_BYTES} bytes")])

    visited = 0
    def walk(value: Any, path: str = "$", depth: int = 0) -> None:
        nonlocal visited
        visited += 1
        if visited > _MAX_NODES:
            if visited == _MAX_NODES + 1:
                errors.append(_err(path, f"configuration exceeds {_MAX_NODES} values"))
            return
        if depth > 16:
            errors.append(_err(path, "maximum configuration nesting depth is 16"))
            return
        if isinstance(value, dict):
            if len(value) > _MAX_CONTAINER_ITEMS:
                errors.append(_err(path, f"object may contain at most {_MAX_CONTAINER_ITEMS} fields"))
                return
            for key, child in value.items():
                if not isinstance(key, str):
                    errors.append(_err(path, "object keys must be strings"))
                    continue
                if str(key).lower() in _FORBIDDEN_KEYS:
                    errors.append(_err(f"{path}.{key}", "field is not permitted; scope and model deployment are server-owned"))
                walk(child, f"{path}.{key}", depth + 1)
        elif isinstance(value, list):
            if len(value) > _MAX_CONTAINER_ITEMS:
                errors.append(_err(path, f"array may contain at most {_MAX_CONTAINER_ITEMS} items"))
                return
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]", depth + 1)
        elif isinstance(value, str):
            if len(value) > _MAX_STRING_CHARS:
                errors.append(_err(path, f"strings may contain at most {_MAX_STRING_CHARS} characters"))
            if "://" in value or "${" in value or "{{" in value:
                errors.append(_err(path, "URLs, templates, and variable interpolation are not supported"))
        elif isinstance(value, int) and not isinstance(value, bool) and not _MIN_INTEGER <= value <= _MAX_INTEGER:
            errors.append(_err(path, "integer is outside the supported signed 64-bit range"))
        elif isinstance(value, float) and not math.isfinite(value):
            errors.append(_err(path, "numbers must be finite"))
        elif value is not None and not isinstance(value, (str, int, float, bool)):
            errors.append(_err(path, "value is not a JSON scalar or container"))

    walk(raw)
    allowed_top = {"schema_version", "config_id", "version", "identity", "questions", "taxonomy", "front_gates", "resolver", "confidence", "runtime", "output"}
    for key in raw:
        if key not in allowed_top:
            errors.append(_err(f"$.{key}", "unknown or unsupported field"))
    identity = raw.get("identity", {})
    if not isinstance(identity, dict):
        identity = {}
        errors.append(_err("$.identity", "must be an object"))
    for key in identity:
        if key not in {"use_case_id", "display_name", "default_locale"}:
            errors.append(_err(f"$.identity.{key}", "unknown field"))
    config_id = raw.get("config_id")
    use_case_id = identity.get("use_case_id")
    if not isinstance(config_id, str) or not _ID.fullmatch(config_id):
        errors.append(_err("$.config_id", "must be a 2–128 character identifier"))
        config_id = "invalid"
    if not isinstance(use_case_id, str) or not _ID.fullmatch(use_case_id):
        errors.append(_err("$.identity.use_case_id", "must be a 2–128 character use-case identifier"))
        use_case_id = "invalid"
    display_name = identity.get("display_name", config_id)
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 160:
        errors.append(_err("$.identity.display_name", "must be a nonempty string up to 160 characters"))
        display_name = config_id
    if "default_locale" in identity and (not isinstance(identity["default_locale"], str) or len(identity["default_locale"]) > 32):
        errors.append(_err("$.identity.default_locale", "must be a locale string up to 32 characters"))
    version = raw.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or not 1 <= version <= 2**31 - 1:
        errors.append(_err("$.version", "must be a positive PostgreSQL integer"))
        version = 1
    if raw.get("schema_version", "1.0.0") != "1.0.0":
        errors.append(_err("$.schema_version", "unsupported schema version"))

    questions = raw.get("questions")
    if not isinstance(questions, dict) or not questions:
        errors.append(_err("$.questions", "must contain at least one typed question"))
        questions = {}
    else:
        for qid, question in questions.items():
            if not isinstance(qid, str) or not _ID.fullmatch(qid) or not isinstance(question, dict):
                errors.append(_err(f"$.questions.{qid}", "invalid question identifier or definition"))
                continue
            qtype = question.get("type")
            if not isinstance(qtype, str) or qtype not in _SIGNAL_TYPES:
                errors.append(_err(f"$.questions.{qid}.type", "must be noul, choice, or score"))
                continue
            if set(question) - {"type", "instructions", "criteria"}:
                errors.append(_err(f"$.questions.{qid}", "contains unsupported fields"))
            if not isinstance(question.get("instructions"), str) or not question["instructions"].strip() or len(question["instructions"]) > 2000:
                errors.append(_err(f"$.questions.{qid}.instructions", "must be nonempty and at most 2000 characters"))
            criteria = question.get("criteria")
            if qtype == "noul" and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"} or any(not isinstance(v, str) or not v.strip() for v in criteria.values())):
                errors.append(_err(f"$.questions.{qid}.criteria", "noul criteria require nonempty true and false descriptions"))
            if qtype == "choice" and (not isinstance(criteria, dict) or len(criteria) < 2 or any(not isinstance(k, str) for k in criteria)):
                errors.append(_err(f"$.questions.{qid}.criteria", "choice criteria must name at least two choices"))
            if qtype == "choice" and isinstance(criteria, dict) and (len(criteria) > 64 or any(not isinstance(k, str) or not k or len(k) > 128 for k in criteria)):
                errors.append(_err(f"$.questions.{qid}.criteria", "choice identifiers must be nonempty, at most 128 characters, and limited to 64 choices"))
            if qtype == "choice" and isinstance(criteria, dict) and any(not isinstance(v, str) or not v.strip() for v in criteria.values()):
                errors.append(_err(f"$.questions.{qid}.criteria", "choice descriptions must be nonempty strings"))
            if qtype == "score" and (not isinstance(criteria, list) or len(criteria) < 2 or len(criteria) > 10):
                errors.append(_err(f"$.questions.{qid}.criteria", "score criteria must contain 2–10 ordered labels"))
            if qtype == "score" and isinstance(criteria, list) and any(not isinstance(v, str) or not v.strip() for v in criteria):
                errors.append(_err(f"$.questions.{qid}.criteria", "score labels must be nonempty strings"))

    taxonomy_raw = raw.get("taxonomy", {})
    if not isinstance(taxonomy_raw, dict):
        errors.append(_err("$.taxonomy", "must be an object")); taxonomy_raw = {}
    elif set(taxonomy_raw) - {"codes"}:
        errors.append(_err("$.taxonomy", "only codes is supported"))
    codes_raw = taxonomy_raw.get("codes", [])
    taxonomy: dict[str, dict[str, Any]] = {}
    if not isinstance(codes_raw, list) or not codes_raw:
        errors.append(_err("$.taxonomy.codes", "must contain at least one code"))
        codes_raw = []
    for i, item in enumerate(codes_raw):
        path = f"$.taxonomy.codes[{i}]"
        if not isinstance(item, dict) or set(item) - {"code", "name", "definition", "level", "parent_code", "terminal", "callback", "success"}:
            errors.append(_err(path, "invalid taxonomy entry"))
            continue
        code = item.get("code")
        if not isinstance(code, str) or not _ID.fullmatch(code) or code in taxonomy:
            errors.append(_err(f"{path}.code", "must be a unique identifier"))
            continue
        taxonomy[code] = item
        if not isinstance(item.get("name"), str) or not item["name"].strip() or len(item["name"]) > 160:
            errors.append(_err(f"{path}.name", "must be a nonempty name up to 160 characters"))
        if "definition" in item and (not isinstance(item["definition"], str) or len(item["definition"]) > 2000):
            errors.append(_err(f"{path}.definition", "must be a string up to 2000 characters"))
        if isinstance(item.get("level"), bool) or not isinstance(item.get("level"), int) or item["level"] < 0:
            errors.append(_err(f"{path}.level", "must be a nonnegative integer"))
        if item.get("parent_code") is not None and not isinstance(item.get("parent_code"), str):
            errors.append(_err(f"{path}.parent_code", "must be a code or null"))
        if any(not isinstance(item.get(flag), bool) for flag in ("terminal", "callback", "success")):
            errors.append(_err(path, "terminal, callback, and success must be booleans"))
    for code, item in taxonomy.items():
        parent = item.get("parent_code")
        if parent is not None and (not isinstance(parent, str) or parent not in taxonomy):
            errors.append(_err(f"$.taxonomy.{code}.parent_code", "must reference an existing code"))
        elif parent is not None and isinstance(item.get("level"), int) and isinstance(taxonomy[parent].get("level"), int) and item["level"] <= taxonomy[parent]["level"]:
            errors.append(_err(f"$.taxonomy.{code}.level", "must be greater than its parent level"))
        seen = {code}
        while isinstance(parent, str) and parent in taxonomy:
            if parent in seen:
                errors.append(_err(f"$.taxonomy.{code}.parent_code", "taxonomy parent links must be acyclic"))
                break
            seen.add(parent)
            parent = taxonomy[parent].get("parent_code")

    resolver = raw.get("resolver", {})
    if not isinstance(resolver, dict) or set(resolver) - {"strategy", "default_emit", "rules"}:
        errors.append(_err("$.resolver", "must contain only strategy, default_emit, and rules"))
        resolver = {}
    if resolver.get("strategy", "FIRST_MATCH_WINS") != "FIRST_MATCH_WINS":
        errors.append(_err("$.resolver.strategy", "only FIRST_MATCH_WINS is supported"))
    default_emit = resolver.get("default_emit")
    if not isinstance(default_emit, str) or default_emit not in taxonomy:
        errors.append(_err("$.resolver.default_emit", "must reference a taxonomy code"))
        default_emit = next(iter(taxonomy), "REVIEW")
    rules_raw = resolver.get("rules", [])
    if not isinstance(rules_raw, list):
        errors.append(_err("$.resolver.rules", "must be an array"))
        rules_raw = []
    rules = []
    ids, priorities = set(), set()

    def check_tree(node: Any, path: str, depth: int = 0) -> None:
        if depth > 16:
            errors.append(_err(path, "condition nesting depth may not exceed 16")); return
        if not isinstance(node, dict):
            errors.append(_err(path, "must be a predicate or all/any/not condition")); return
        if set(node) == {"all"} or set(node) == {"any"}:
            entries = node[next(iter(node))]
            if not isinstance(entries, list) or not entries or len(entries) > 32:
                errors.append(_err(path, "condition list must contain 1–32 entries")); return
            for j, child in enumerate(entries): check_tree(child, f"{path}[{j}]", depth + 1)
        elif set(node) == {"not"}:
            check_tree(node["not"], f"{path}.not", depth + 1)
        elif set(node) <= {"signal", "operator", "value"} and "signal" in node and "operator" in node:
            signal_name, operator = node["signal"], node["operator"]
            if not isinstance(signal_name, str) or signal_name not in questions: errors.append(_err(f"{path}.signal", "must reference a configured question"))
            if not isinstance(operator, str) or operator not in _OPERATORS: errors.append(_err(f"{path}.operator", "unsupported predicate operator"))
            elif operator != "EXISTS" and "value" not in node: errors.append(_err(path, "operator requires a value"))
            elif isinstance(signal_name, str) and signal_name in questions and operator.startswith("NOUL_") and questions[signal_name].get("type") != "noul": errors.append(_err(path, "NOUL operator requires a noul question"))
            elif isinstance(signal_name, str) and signal_name in questions and operator.startswith("CHOICE_") and questions[signal_name].get("type") != "choice": errors.append(_err(path, "CHOICE operator requires a choice question"))
            elif isinstance(signal_name, str) and signal_name in questions and operator.startswith("SCORE_") and questions[signal_name].get("type") != "score": errors.append(_err(path, "SCORE operator requires a score question"))
            elif operator in {"NOUL_GTE", "NOUL_LTE", "SCORE_GTE", "SCORE_LTE", "CHOICE_CONFIDENCE_GTE"}:
                _bounded_number(node.get("value"), f"{path}.value", errors)
            elif operator == "CHOICE_EQ" and isinstance(signal_name, str) and signal_name in questions:
                criteria = questions[signal_name].get("criteria")
                if not isinstance(criteria, dict) or not isinstance(node.get("value"), str) or node.get("value") not in criteria:
                    errors.append(_err(f"{path}.value", "must be a configured choice"))
            elif operator == "CHOICE_IN" and isinstance(signal_name, str) and signal_name in questions:
                criteria = questions[signal_name].get("criteria")
                if not isinstance(criteria, dict) or not isinstance(node.get("value"), list) or not node["value"] or any(not isinstance(choice, str) or choice not in criteria for choice in node["value"]): errors.append(_err(f"{path}.value", "must be a nonempty list of configured choices"))
        else: errors.append(_err(path, "unsupported condition shape"))

    for i, rule in enumerate(rules_raw):
        path = f"$.resolver.rules[{i}]"
        if not isinstance(rule, dict) or set(rule) - {"id", "priority", "when", "emit"}:
            errors.append(_err(path, "invalid rule fields")); continue
        rid, priority, emit = rule.get("id"), rule.get("priority"), rule.get("emit")
        if not isinstance(rid, str) or not _ID.fullmatch(rid) or rid in ids: errors.append(_err(f"{path}.id", "must be unique identifier"))
        if isinstance(priority, bool) or not isinstance(priority, int) or priority in priorities: errors.append(_err(f"{path}.priority", "must be a unique integer"))
        if not isinstance(emit, str) or emit not in taxonomy: errors.append(_err(f"{path}.emit", "must reference a taxonomy code"))
        if isinstance(rid, str): ids.add(rid)
        if isinstance(priority, int) and not isinstance(priority, bool): priorities.add(priority)
        check_tree(rule.get("when"), f"{path}.when")
        rules.append(rule)
    rules.sort(key=lambda rule: rule.get("priority") if isinstance(rule.get("priority"), int) and not isinstance(rule.get("priority"), bool) else 0)

    gates_raw = raw.get("front_gates", [])
    if not isinstance(gates_raw, list):
        errors.append(_err("$.front_gates", "must be an array")); gates_raw = []
    gates = []
    seen_gate_ids, seen_gate_prio = set(), set()
    for i, gate in enumerate(gates_raw):
        path = f"$.front_gates[{i}]"
        if not isinstance(gate, dict) or set(gate) != {"id", "enabled", "priority", "source", "condition", "emit"}:
            errors.append(_err(path, "unsupported front gate shape")); continue
        cond = gate.get("condition", {})
        if not isinstance(gate.get("source"), str) or gate.get("source") not in {"telephony_status", "call_status"}:
            errors.append(_err(f"{path}.source", "only authoritative server facts are supported"))
        if not isinstance(cond, dict) or set(cond) != {"operator", "value"} or not isinstance(cond.get("operator"), str) or cond.get("operator") not in {"EQ", "NE", "IN", "NOT_IN", "EXISTS"}:
            errors.append(_err(f"{path}.condition", "unsupported condition"))
        elif cond["operator"] in {"EQ", "NE"} and (not isinstance(cond["value"], str) or not cond["value"].strip()):
            errors.append(_err(f"{path}.condition.value", "EQ/NE require a nonempty authoritative fact value"))
        elif cond["operator"] in {"IN", "NOT_IN"} and (not isinstance(cond["value"], list) or not cond["value"] or any(not isinstance(v, str) or not v.strip() for v in cond["value"])):
            errors.append(_err(f"{path}.condition.value", "IN/NOT_IN require a nonempty list of authoritative fact values"))
        if not isinstance(gate.get("id"), str) or not _ID.fullmatch(gate["id"]): errors.append(_err(f"{path}.id", "must be an identifier"))
        if not isinstance(gate.get("enabled"), bool): errors.append(_err(f"{path}.enabled", "must be a boolean"))
        if isinstance(gate.get("priority"), bool) or not isinstance(gate.get("priority"), int): errors.append(_err(f"{path}.priority", "must be an integer"))
        if not isinstance(gate.get("emit"), str) or gate.get("emit") not in taxonomy: errors.append(_err(f"{path}.emit", "must reference a taxonomy code"))
        if isinstance(gate.get("id"), str) and gate["id"] in seen_gate_ids or isinstance(gate.get("priority"), int) and gate["priority"] in seen_gate_prio: errors.append(_err(path, "gate IDs and priorities must be unique"))
        if isinstance(gate.get("id"), str): seen_gate_ids.add(gate["id"])
        if isinstance(gate.get("priority"), int) and not isinstance(gate.get("priority"), bool): seen_gate_prio.add(gate["priority"])
        gates.append(gate)
    gates.sort(key=lambda gate: gate.get("priority") if isinstance(gate.get("priority"), int) and not isinstance(gate.get("priority"), bool) else 0)

    confidence = raw.get("confidence", {})
    if not isinstance(confidence, dict) or set(confidence) - {"default_commit_threshold", "default_review_threshold", "low_confidence_action", "per_code"}:
        errors.append(_err("$.confidence", "unsupported confidence policy")); confidence = {}
    commit = _bounded_number(confidence.get("default_commit_threshold", 0.65), "$.confidence.default_commit_threshold", errors)
    review = _bounded_number(confidence.get("default_review_threshold", 0.45), "$.confidence.default_review_threshold", errors)
    if commit is not None and review is not None and review > commit: errors.append(_err("$.confidence", "review threshold must not exceed commit threshold"))
    if confidence.get("low_confidence_action", "REVIEW") != "REVIEW": errors.append(_err("$.confidence.low_confidence_action", "only REVIEW is supported"))
    per_code = {}
    per_code_raw = confidence.get("per_code", {})
    if not isinstance(per_code_raw, dict):
        errors.append(_err("$.confidence.per_code", "must be an object")); per_code_raw = {}
    for code, values in per_code_raw.items():
        if code not in taxonomy: errors.append(_err(f"$.confidence.per_code.{code}", "unknown taxonomy code")); continue
        threshold = _bounded_number(values.get("commit_threshold") if isinstance(values, dict) else None, f"$.confidence.per_code.{code}.commit_threshold", errors)
        if threshold is not None: per_code[code] = threshold
    runtime = raw.get("runtime", {})
    if not isinstance(runtime, dict) or set(runtime) - {"context_limit_chars", "window_chars", "overlap_turns", "max_chunks"}:
        errors.append(_err("$.runtime", "unsupported runtime settings")); runtime = {}
    context_limit = runtime.get("context_limit_chars", 100_000)
    window_chars = runtime.get("window_chars", 12_000)
    overlap = runtime.get("overlap_turns", 1)
    max_chunks = runtime.get("max_chunks", 128)
    if isinstance(context_limit, bool) or not isinstance(context_limit, int) or not 1_000 <= context_limit <= 2_000_000: errors.append(_err("$.runtime.context_limit_chars", "must be 1000–2000000")); context_limit=100_000
    if isinstance(window_chars, bool) or not isinstance(window_chars, int) or not 500 <= window_chars <= min(context_limit, 100_000): errors.append(_err("$.runtime.window_chars", "must be 500–100000 and no larger than context limit")); window_chars=min(12_000, context_limit)
    if isinstance(overlap, bool) or not isinstance(overlap, int) or not 0 <= overlap <= 10: errors.append(_err("$.runtime.overlap_turns", "must be 0–10")); overlap=1
    if isinstance(max_chunks, bool) or not isinstance(max_chunks, int) or not 1 <= max_chunks <= 128: errors.append(_err("$.runtime.max_chunks", "must be 1–128")); max_chunks=128

    output = raw.get("output")
    if output is not None:
        if not isinstance(output, dict) or set(output) - {"include", "aliases"}:
            errors.append(_err("$.output", "only include and aliases are supported"))
        elif ("include" in output and (not isinstance(output["include"], list) or len(output["include"]) > 32 or any(not isinstance(item, str) for item in output["include"]))
              or "aliases" in output and (not isinstance(output["aliases"], dict) or len(output["aliases"]) > 32 or any(not isinstance(k, str) or not isinstance(v, str) for k, v in output["aliases"].items()))):
            errors.append(_err("$.output", "include and aliases must be bounded string collections"))

    if errors:
        raise ConfigError(errors)
    normalized = json.loads(json.dumps(raw, sort_keys=True, separators=(",", ":")))
    digest = hashlib.sha256(json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    return CompiledDispositionConfig(config_id, use_case_id, version, display_name, questions, taxonomy, tuple(gates), tuple(rules), default_emit, commit if commit is not None else 0.65, review if review is not None else 0.45, per_code, context_limit, window_chars, overlap, max_chunks, normalized, digest)
