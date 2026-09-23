"""Deterministic evaluator for bounded, redacted synthetic/adjudicated JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

MAX_DATASET_BYTES = 16 * 1024 * 1024
MAX_DATASET_LINE_BYTES = 64 * 1024
MAX_DATASET_CASES = 10_000
MAX_FINDINGS_PER_CASE = 256
MAX_DIMENSIONS_PER_CASE = 32
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_RECORD_KEYS = {"case_id", "excluded", "abstained", "gold_findings", "predicted_findings", "gold_dimensions", "predicted_dimensions"}
_PREDICTION_STATUSES = {"PENDING", "SATISFIED", "POTENTIAL_VIOLATION", "UNKNOWN", "ADVISORY"}


class EvaluationInputError(ValueError):
    """Dataset does not match the bounded evaluation contract."""


def precision_recall(tp: int, fp: int, fn: int) -> dict[str, float | None]:
    values = (tp, fp, fn)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("confusion counts must be non-negative integers")
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {"precision": precision, "recall": recall}


def _validate_record(record: Any, case_ids: set[str]) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != _RECORD_KEYS:
        raise EvaluationInputError("record has unsupported or missing fields")
    case_id = record["case_id"]
    if not isinstance(case_id, str) or not case_id.startswith("case-") or not _IDENTIFIER.fullmatch(case_id) or case_id in case_ids:
        raise EvaluationInputError("case_id must be a unique pseudonymous case-* identifier")
    case_ids.add(case_id)
    for flag in ("excluded", "abstained"):
        if not isinstance(record[flag], bool):
            raise EvaluationInputError(f"{flag} must be boolean")
    if record["excluded"] and record["abstained"]:
        raise EvaluationInputError("explicitly excluded cases must not also be marked as evaluated abstentions")
    for name in ("gold_findings", "predicted_findings"):
        values = record[name]
        if not isinstance(values, list) or len(values) > MAX_FINDINGS_PER_CASE:
            raise EvaluationInputError(f"{name} must be a bounded list")
        seen: set[str] = set()
        for finding in values:
            if not isinstance(finding, dict):
                raise EvaluationInputError("finding entry must be an object")
            required = {"rule_id", "critical"} if name == "gold_findings" else {"rule_id", "status"}
            if set(finding) != required:
                raise EvaluationInputError("finding entry has unsupported or missing fields")
            rule_id = finding["rule_id"]
            if not isinstance(rule_id, str) or not _IDENTIFIER.fullmatch(rule_id) or rule_id in seen:
                raise EvaluationInputError("rule identifiers must be unique bounded identifiers per case")
            seen.add(rule_id)
            if name == "gold_findings":
                if not isinstance(finding["critical"], bool):
                    raise EvaluationInputError("gold critical label must be boolean")
            elif not isinstance(finding["status"], str) or finding["status"] not in _PREDICTION_STATUSES:
                raise EvaluationInputError("predicted finding status is invalid")
    for name in ("gold_dimensions", "predicted_dimensions"):
        values = record[name]
        if not isinstance(values, dict) or len(values) > MAX_DIMENSIONS_PER_CASE:
            raise EvaluationInputError(f"{name} must be a bounded object")
        for dimension, score in values.items():
            if not isinstance(dimension, str) or not _IDENTIFIER.fullmatch(dimension):
                raise EvaluationInputError("dimension identifier is invalid")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise EvaluationInputError("dimension scores must be finite numbers from 1 through 5")
            try:
                finite_score = math.isfinite(score)
            except (OverflowError, TypeError):
                finite_score = False
            if not finite_score or not 1 <= score <= 5:
                raise EvaluationInputError("dimension scores must be finite numbers from 1 through 5")
    return record


def evaluate_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(records, list) or len(records) > MAX_DATASET_CASES:
        raise EvaluationInputError(f"evaluation dataset must contain at most {MAX_DATASET_CASES} records")
    ids: set[str] = set()
    validated = [_validate_record(record, ids) for record in records]
    eligible = [record for record in validated if not record["excluded"]]
    abstained = sum(record["abstained"] for record in eligible)
    coverage_count = len(eligible) - abstained
    counters: dict[str, dict[str, int]] = {}
    critical_total = 0
    missed_critical = 0
    absolute_errors: list[float] = []
    exact_scores = 0
    applicable_dimensions = 0
    excluded_dimensions = 0

    for record in eligible:
        gold = {item["rule_id"]: item["critical"] for item in record["gold_findings"]}
        predicted = {item["rule_id"] for item in record["predicted_findings"] if item["status"] == "POTENTIAL_VIOLATION"}
        for rule_id in set(gold) | predicted:
            stat = counters.setdefault(rule_id, {"tp": 0, "fp": 0, "fn": 0, "critical_gold": 0, "critical_missed": 0})
            expected = rule_id in gold
            actual = rule_id in predicted
            stat["tp"] += int(expected and actual)
            stat["fp"] += int(not expected and actual)
            stat["fn"] += int(expected and not actual)
            critical = bool(gold.get(rule_id, False))
            stat["critical_gold"] += int(critical)
            stat["critical_missed"] += int(critical and not actual)
            critical_total += int(critical)
            missed_critical += int(critical and not actual)
        for dimension, expected_score in record["gold_dimensions"].items():
            applicable_dimensions += 1
            if record["abstained"]:
                excluded_dimensions += 1
                continue
            actual_score = record["predicted_dimensions"].get(dimension)
            if actual_score is None:
                excluded_dimensions += 1
                continue
            error = abs(float(actual_score) - float(expected_score))
            absolute_errors.append(error)
            exact_scores += int(error == 0)

    rule_report = {}
    for rule_id in sorted(counters):
        stat = counters[rule_id]
        rule_report[rule_id] = {**stat, **precision_recall(stat["tp"], stat["fp"], stat["fn"])}
    dimension_count = len(absolute_errors)
    return {
        "schema_version": "evaluation-report-v1",
        "sample_counts": {
            "total": len(validated), "explicitly_excluded": len(validated) - len(eligible),
            "eligible": len(eligible), "abstained": abstained, "covered": coverage_count,
        },
        "coverage": coverage_count / len(eligible) if eligible else None,
        "findings_by_rule": rule_report,
        "critical_findings": {"gold": critical_total, "missed": missed_critical},
        "missed_critical_findings": missed_critical,
        "score_agreement": {
            "applicable_dimensions": applicable_dimensions,
            "adjudicated_dimensions": dimension_count,
            "excluded_dimensions": excluded_dimensions,
            "mean_absolute_error": sum(absolute_errors) / dimension_count if dimension_count else None,
            "exact_matches": exact_scores,
            "exact_agreement": exact_scores / dimension_count if dimension_count else None,
        },
    }


def load_dataset(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    target = Path(path)
    try:
        with target.open("rb") as source:
            raw = source.read(MAX_DATASET_BYTES + 1)
    except OSError as error:
        raise EvaluationInputError("dataset could not be read") from error
    if len(raw) > MAX_DATASET_BYTES:
        raise EvaluationInputError(f"dataset exceeds {MAX_DATASET_BYTES} byte limit")
    digest = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EvaluationInputError("dataset must be UTF-8 JSONL") from error
    records = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        if len(line.encode("utf-8")) > MAX_DATASET_LINE_BYTES:
            raise EvaluationInputError(f"line {line_number} exceeds the record byte limit")
        if len(records) >= MAX_DATASET_CASES:
            raise EvaluationInputError(f"dataset exceeds {MAX_DATASET_CASES} case limit")
        try:
            value = json.loads(line)
        except (ValueError, RecursionError) as error:
            raise EvaluationInputError(f"line {line_number} is not valid JSON") from error
        records.append(value)
    # Validate once before evaluation so malformed input produces no partial report.
    evaluate_records(records)
    return records, digest


def evaluate_dataset(path: str | Path) -> dict[str, Any]:
    records, digest = load_dataset(path)
    return {**evaluate_records(records), "dataset_sha256": digest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate redacted, adjudicated JSONL without changing expected labels.")
    parser.add_argument("--dataset", required=True, help="bounded JSONL file containing synthetic or approved redacted labels")
    parser.add_argument("--output", required=True, help="path for deterministic JSON report")
    args = parser.parse_args(argv)
    try:
        report = evaluate_dataset(args.dataset)
        output = json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        Path(args.output).write_text(output, encoding="utf-8", newline="\n")
    except (EvaluationInputError, OSError) as error:
        print(f"evaluation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
