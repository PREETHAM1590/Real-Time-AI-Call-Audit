"""Tenant-scoped score reporting and bounded findings export."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.auth import Scope
from app.privacy import redact_text

MAX_REPORT_DAYS = 366
MAX_EXPORT_DAYS = 92
MAX_EXPORT_ROWS = 10_000
MAX_OWN_SCORE_ROWS = 500
MIN_TEAM_AGENTS = 5


class ReportForbidden(PermissionError):
    """The requested report is outside the caller's role or team scope."""


class ReportLimitError(ValueError):
    """The report date window or result limit exceeds the bounded contract."""


class ReportPrivacyUnavailable(RuntimeError):
    """A note could not be redacted; do not return the affected report."""


def csv_cell(value: str) -> str:
    """Neutralize spreadsheet formulas; csv.writer handles quoting separately."""
    if not isinstance(value, str):
        raise TypeError("CSV cells must be strings")
    stripped = value.lstrip()
    return "'" + value if stripped.startswith(("=", "+", "-", "@")) else value


def _window(start: datetime, end: datetime, *, max_days: int) -> tuple[datetime, datetime]:
    if not isinstance(start, datetime) or not isinstance(end, datetime) or start.tzinfo is None or end.tzinfo is None:
        raise ReportLimitError("Report times must be timezone-aware")
    start_utc, end_utc = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    if end_utc <= start_utc or end_utc - start_utc > timedelta(days=max_days):
        raise ReportLimitError(f"Report range must be positive and no longer than {max_days} days")
    return start_utc, end_utc


def _redact_note(text: Any, redact: Callable[[str], str]) -> str:
    if not isinstance(text, str) or len(text) > 10_000:
        raise ReportPrivacyUnavailable("Report note is invalid")
    try:
        safe = redact(text)
    except Exception:
        raise ReportPrivacyUnavailable("Report note could not be redacted") from None
    if not isinstance(safe, str) or len(safe) > 10_000:
        raise ReportPrivacyUnavailable("Report note could not be redacted")
    return safe


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def own_scores(connection, scope: Scope, *, redact: Callable[[str], str] = redact_text) -> list[dict[str, Any]]:
    """Return only the authenticated agent's reviewed, current audit records."""
    if scope.role != "AGENT" or not scope.user_id.strip() or not scope.organisation_id.strip():
        raise ReportForbidden("Agent identity required")
    rows = connection.execute(
        "SELECT c.id,c.agent_id,c.team_id,c.created_at,c.processing_state,a.revision,a.transcript_revision,"
        "a.rubric_version,a.rubric_hash,a.model_artifact,a.inference_runtime,a.overall_score,a.decision,"
        "a.dimensions_json,a.coaching_narrative,a.highlights,a.improvement_areas,"
        "r.version,r.action,r.effective_scores_json,r.effective_score,r.effective_decision,r.reason "
        "FROM calls c JOIN audits a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
        "JOIN LATERAL (SELECT version,action,effective_scores_json,effective_score,effective_decision,reason "
        "FROM reviews latest_review WHERE latest_review.organisation_id=a.organisation_id AND latest_review.audit_id=a.id "
        "ORDER BY version DESC LIMIT 1) r ON TRUE "
        "WHERE c.organisation_id=%s AND c.agent_id=%s AND c.tombstoned_at IS NULL "
        "AND c.transcript_revision=a.transcript_revision "
        "AND a.revision=(SELECT max(current.revision) FROM audits current WHERE current.organisation_id=a.organisation_id AND current.call_id=a.call_id) "
        "AND r.effective_score IS NOT NULL AND r.action IN ('ACCEPT','OVERRIDE') "
        "ORDER BY c.created_at DESC,a.id LIMIT %s",
        (scope.organisation_id, scope.user_id, MAX_OWN_SCORE_ROWS),
    ).fetchall()
    result = []
    for row in rows:
        dimensions = row[13] if isinstance(row[13], list) else []
        reviewed_scores = row[19] if isinstance(row[19], dict) else {}
        checklist = []
        for dimension in dimensions:
            if not isinstance(dimension, dict):
                continue
            checklist.append({
                "id": dimension.get("id"),
                "status": dimension.get("status"),
                "machine_score": dimension.get("score"),
                "reviewed_score": reviewed_scores.get(dimension.get("id")),
                "reason": _redact_note(dimension.get("reason", ""), redact),
            })
        notes = []
        narrative = row[14] if isinstance(row[14], dict) else {}
        if narrative.get("text"):
            notes.append({"kind": "coaching", "source": "machine_audit", "text": _redact_note(narrative["text"], redact)})
        for kind, values in (("highlight", row[15]), ("improvement", row[16])):
            if not isinstance(values, list):
                continue
            for item in values:
                if isinstance(item, dict) and item.get("text"):
                    notes.append({"kind": kind, "source": "machine_audit", "text": _redact_note(item["text"], redact)})
        if row[22]:
            notes.append({"kind": "human_review", "source": "latest_review", "text": _redact_note(row[22], redact)})
        result.append({
            "call_id": str(row[0]), "agent_id": row[1], "team_id": row[2], "created_at": row[3].isoformat(),
            "processing_state": row[4], "audit_revision": row[5], "transcript_revision": row[6],
            "rubric_version": row[7], "rubric_hash": row[8].strip(), "model_artifact": row[9], "inference_runtime": row[10],
            "machine_score": _as_float(row[11]), "machine_decision": row[12], "review_version": row[17],
            "review_action": row[18], "reviewed_score": _as_float(row[20]), "reviewed_decision": row[21],
            "checklist": checklist, "coaching_notes": notes,
        })
    return result


def team_report(connection, scope: Scope, start: datetime, end: datetime) -> dict[str, Any]:
    """Aggregate current machine/review scores for server-authorized teams."""
    if scope.role != "TEAM_LEADER" or not scope.team_ids:
        raise ReportForbidden("Team leader membership required")
    start_utc, end_utc = _window(start, end, max_days=MAX_REPORT_DAYS)
    rows = connection.execute(
        "SELECT c.team_id,a.rubric_version,a.model_artifact,a.inference_runtime,COUNT(*),COUNT(DISTINCT c.agent_id),"
        "COUNT(a.overall_score),AVG(a.overall_score),COUNT(r.effective_score),AVG(r.effective_score),"
        "COUNT(*) FILTER (WHERE a.decision='PASS'),COUNT(*) FILTER (WHERE a.decision='FAIL'),"
        "COUNT(*) FILTER (WHERE r.effective_decision='PASS'),COUNT(*) FILTER (WHERE r.effective_decision='FAIL') "
        "FROM calls c JOIN audits a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
        "LEFT JOIN LATERAL (SELECT effective_score,effective_decision FROM reviews latest_review "
        "WHERE latest_review.organisation_id=a.organisation_id AND latest_review.audit_id=a.id ORDER BY version DESC LIMIT 1) r ON TRUE "
        "WHERE c.organisation_id=%s AND c.team_id=ANY(%s) AND c.tombstoned_at IS NULL "
        "AND c.created_at >= %s AND c.created_at < %s AND c.transcript_revision=a.transcript_revision "
        "AND a.revision=(SELECT max(current.revision) FROM audits current WHERE current.organisation_id=a.organisation_id AND current.call_id=a.call_id) "
        "GROUP BY c.team_id,a.rubric_version,a.model_artifact,a.inference_runtime ORDER BY c.team_id,a.rubric_version,a.model_artifact,a.inference_runtime",
        (scope.organisation_id, sorted(scope.team_ids), start_utc, end_utc),
    ).fetchall()
    cohorts = []
    for row in rows:
        suppressed = int(row[5]) < MIN_TEAM_AGENTS
        cohorts.append({
            "team_id": row[0], "rubric_version": row[1], "model_artifact": row[2], "inference_runtime": row[3],
            "suppressed": suppressed,
            "suppression_reason": "MINIMUM_AGENT_THRESHOLD" if suppressed else None,
            "distinct_agents": None if suppressed else int(row[5]),
            "sample_count": None if suppressed else int(row[4]),
            "machine_sample_count": None if suppressed else int(row[6]),
            "machine_average": None if suppressed else _as_float(row[7]),
            "reviewed_sample_count": None if suppressed else int(row[8]),
            "reviewed_average": None if suppressed else _as_float(row[9]),
            "machine_pass_count": None if suppressed else int(row[10]),
            "machine_fail_count": None if suppressed else int(row[11]),
            "reviewed_pass_count": None if suppressed else int(row[12]),
            "reviewed_fail_count": None if suppressed else int(row[13]),
        })
    return {"period_start": start_utc.isoformat(), "period_end": end_utc.isoformat(), "cohorts": cohorts}


def export_findings(
    connection,
    scope: Scope,
    start: datetime,
    end: datetime,
    *,
    redact: Callable[[str], str] = redact_text,
    limit: int = MAX_EXPORT_ROWS,
) -> tuple[bytes, int]:
    """Create a bounded current-revision findings CSV with no transcript text."""
    if scope.role != "COMPLIANCE_OFFICER":
        raise ReportForbidden("Compliance officer role required")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EXPORT_ROWS:
        raise ReportLimitError("Export limit must be between 1 and the configured maximum")
    start_utc, end_utc = _window(start, end, max_days=MAX_EXPORT_DAYS)
    rows = connection.execute(
        "SELECT c.id,c.team_id,c.created_at,f.rule_id,f.ruleset_version,f.ruleset_hash,f.policy_text_version,"
        "f.status,f.severity,f.evidence_ids,f.deadline_ms,f.remediation "
        "FROM calls c JOIN findings f ON f.organisation_id=c.organisation_id AND f.call_id=c.id "
        "WHERE c.organisation_id=%s AND c.tombstoned_at IS NULL AND c.created_at >= %s AND c.created_at < %s "
        "AND f.transcript_revision=c.transcript_revision ORDER BY c.created_at,c.id,f.rule_id,f.id LIMIT %s",
        (scope.organisation_id, start_utc, end_utc, limit + 1),
    ).fetchall()
    if len(rows) > limit:
        raise ReportLimitError("Export exceeds the configured row limit")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\r\n")
    writer.writerow(("call_id", "team_id", "call_created_at", "rule_id", "ruleset_version", "ruleset_hash", "policy_text_version", "status", "severity", "evidence_ids", "deadline_ms", "remediation"))
    for row in rows:
        # Human-authored labels and notes cross the export boundary only after
        # local redaction; opaque IDs, hashes and timestamps remain identifiers.
        team_id = _redact_note(row[1], redact)
        rule_id = _redact_note(row[3], redact)
        ruleset_version = _redact_note(row[4], redact)
        policy_text_version = _redact_note(row[6], redact)
        remediation = _redact_note(row[11], redact)
        cells = (
            str(row[0]), team_id, row[2].isoformat(), rule_id, ruleset_version, row[5].strip(), policy_text_version, row[7], row[8],
            json_string(row[9]), "" if row[10] is None else str(row[10]), remediation,
        )
        writer.writerow(tuple(csv_cell(value) for value in cells))
    return output.getvalue().encode("utf-8"), len(rows)


def json_string(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
