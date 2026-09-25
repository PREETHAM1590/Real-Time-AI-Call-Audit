"""Tenant-scoped, append-only analyst review revisions."""

from __future__ import annotations

import json
from typing import Any, Mapping
from uuid import UUID, uuid4

from app.audit import DIMENSION_IDS, weighted_score
from app.auth import Scope, can_access
from app.events import append_call_updated


class ReviewConflict(ValueError):
    """The review head or source evidence changed since it was loaded."""


class ReviewNotFound(LookupError):
    """The audit is absent, inaccessible, tombstoned, or outside this tenant."""


class ReviewForbidden(PermissionError):
    """The caller cannot perform analyst review actions."""


def _reviewer(scope: Scope) -> None:
    if scope.role not in {"QA_ANALYST", "ADMIN"}:
        raise ReviewForbidden("QA analyst permission required")


def append_review(
    connection,
    scope: Scope,
    audit_id: str,
    base_review_version: int,
    action: str,
    scores: Mapping[str, int],
    reason: str,
    *,
    request_id: str | None = None,
) -> dict[str, Any]:
    """Append an ACCEPT/OVERRIDE revision after locking its machine audit."""
    _reviewer(scope)
    try:
        audit_uuid = UUID(audit_id)
    except (ValueError, TypeError, AttributeError):
        raise ReviewNotFound("Audit not found") from None
    if isinstance(base_review_version, bool) or not isinstance(base_review_version, int) or not 0 <= base_review_version <= 2**31 - 2:
        raise ValueError("base_review_version must be a nonnegative integer")
    if not isinstance(action, str) or action not in {"ACCEPT", "OVERRIDE", "TRIAGE"}:
        raise ValueError("action must be ACCEPT, OVERRIDE, or TRIAGE")
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 1000:
        raise ValueError("A nonempty review reason of at most 1000 characters is required")
    if not isinstance(scores, Mapping) or len(scores) > len(DIMENSION_IDS):
        raise ValueError("scores must be a mapping of changed dimension scores")
    if set(scores) - set(DIMENSION_IDS):
        raise ValueError("unknown audit dimension")
    if action in {"ACCEPT", "TRIAGE"} and scores:
        raise ValueError(f"{action} cannot include score changes")
    if action == "OVERRIDE" and not scores:
        raise ValueError("OVERRIDE must include at least one changed score")
    if any(isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5 for score in scores.values()):
        raise ValueError("review scores must be integers from 1 to 5")

    with connection.transaction():
        row = connection.execute(
            "SELECT a.call_id,a.revision,a.transcript_revision,a.dimensions_json,a.overall_score,a.decision,a.pass_threshold,"
            "c.agent_id,c.team_id,c.transcript_revision,c.tombstoned_at,c.processing_state,"
            "(SELECT max(revision) FROM audits current WHERE current.organisation_id=a.organisation_id AND current.call_id=a.call_id) "
            "FROM audits a JOIN calls c ON c.organisation_id=a.organisation_id AND c.id=a.call_id "
            "WHERE a.organisation_id=%s AND a.id=%s FOR UPDATE OF a,c",
            (scope.organisation_id, audit_uuid),
        ).fetchone()
        if row is None or row[10] is not None or not can_access(scope, scope.organisation_id, row[7], row[8]):
            raise ReviewNotFound("Audit not found")
        if row[2] != row[9] or row[1] != row[12]:
            raise ReviewConflict("Audit evidence is superseded; reload the current transcript")
        head = connection.execute(
            "SELECT version,effective_scores_json,effective_score,effective_decision,action FROM reviews "
            "WHERE organisation_id=%s AND audit_id=%s ORDER BY version DESC LIMIT 1",
            (scope.organisation_id, audit_uuid),
        ).fetchone()
        current = head[0] if head else 0
        if current != base_review_version:
            raise ReviewConflict("A newer review exists; reload before saving")

        if action == "TRIAGE":
            if row[5] != "NEEDS_REVIEW":
                raise ValueError("TRIAGE is only valid for an audit already requiring review")
            if head is not None and head[4] == "TRIAGE":
                raise ReviewConflict("This audit is already triaged and remains in the review queue")
            effective_scores = {}
            effective_score = None
            effective_decision = "NEEDS_REVIEW"
        else:
            machine_dimensions = row[3]
            if not isinstance(machine_dimensions, list) or len(machine_dimensions) != len(DIMENSION_IDS):
                raise ReviewConflict("This audit has no scoreable dimensions")
            machine_scores: dict[str, int | str | None] = {}
            for dimension in machine_dimensions:
                if not isinstance(dimension, Mapping) or not isinstance(dimension.get("id"), str) or dimension["id"] not in DIMENSION_IDS:
                    raise ReviewConflict("This audit has invalid dimensions")
                status, score = dimension.get("status"), dimension.get("score")
                if status == "SCORED":
                    machine_scores[dimension["id"]] = score
                elif status == "NOT_APPLICABLE":
                    machine_scores[dimension["id"]] = "NOT_APPLICABLE"
                else:
                    machine_scores[dimension["id"]] = None
            current_scores = head[1] if head else machine_scores

        if action == "ACCEPT":
            if (head is None and row[4] is None) or row[5] == "NEEDS_REVIEW":
                raise ReviewConflict("An incomplete audit cannot be accepted as scored")
            # ACCEPT confirms the currently displayed score vector: the machine
            # score on the first review, or the latest reviewed vector thereafter.
            effective_scores = current_scores
            effective_score = float(head[2] if head else row[4])
            effective_decision = head[3] if head else row[5]
        elif action == "OVERRIDE":
            if row[4] is None or row[5] == "NEEDS_REVIEW":
                raise ReviewConflict("An incomplete audit cannot receive a score override")
            if row[6] is None:
                raise ReviewConflict("This legacy audit has no persisted rubric threshold")
            for dimension_id in scores:
                if current_scores[dimension_id] in (None, "NOT_APPLICABLE"):
                    raise ValueError("Only scored dimensions may be overridden")
            effective_scores = {**current_scores, **dict(scores)}
            try:
                effective_score = weighted_score(effective_scores)
            except ValueError as error:
                raise ReviewConflict("Audit score vector is incomplete") from error
            unresolved_critical = connection.execute(
                "SELECT 1 FROM findings WHERE organisation_id=%s AND call_id=%s AND transcript_revision=%s "
                "AND severity='HIGH' AND status IN ('UNKNOWN','POTENTIAL_VIOLATION') LIMIT 1",
                (scope.organisation_id, row[0], row[2]),
            ).fetchone() is not None
            effective_decision = "NEEDS_REVIEW" if unresolved_critical else ("PASS" if effective_score >= row[6] else "FAIL")

        version = base_review_version + 1
        review_uuid = uuid4()
        connection.execute(
            "INSERT INTO reviews(organisation_id,id,audit_id,call_id,audit_revision,version,base_review_version,reviewer_id,action,changed_scores_json,effective_scores_json,effective_score,effective_decision,reason) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s)",
            (scope.organisation_id, review_uuid, audit_uuid, row[0], row[1], version, base_review_version, scope.user_id, action, json.dumps(dict(scores), sort_keys=True), json.dumps(effective_scores, sort_keys=True), effective_score, effective_decision, reason.strip()),
        )
        connection.execute(
            "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome,request_id) "
            "VALUES (%s,%s,%s,'REVIEW_SAVED','AUDIT_REVIEW',%s,'SUCCESS',%s)",
            (scope.organisation_id, uuid4(), scope.user_id, audit_uuid, request_id),
        )
        append_call_updated(connection, scope.organisation_id, str(row[0]), row[11], row[9])
    return {
        "id": str(review_uuid), "audit_id": str(audit_uuid), "version": version,
        "base_review_version": base_review_version, "reviewer_id": scope.user_id,
        "action": action, "changed_scores": dict(scores), "effective_scores": effective_scores,
        "effective_score": effective_score, "effective_decision": effective_decision,
        "reason": reason.strip(),
    }


def review_queue(connection, scope: Scope, *, limit: int = 50) -> list[dict[str, Any]]:
    _reviewer(scope)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    rows = connection.execute(
        "SELECT c.id,a.id,a.decision,a.overall_score,c.processing_state,c.agent_id,c.team_id,c.created_at,a.transcript_revision,a.revision "
        "FROM calls c JOIN audits a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
        "WHERE c.organisation_id=%s AND c.tombstoned_at IS NULL AND c.transcript_revision=a.transcript_revision "
        "AND a.revision=(SELECT max(latest.revision) FROM audits latest WHERE latest.organisation_id=a.organisation_id AND latest.call_id=a.call_id) "
        "AND NOT EXISTS (SELECT 1 FROM reviews r WHERE r.organisation_id=a.organisation_id AND r.audit_id=a.id "
        "AND r.version=(SELECT max(latest_review.version) FROM reviews latest_review WHERE latest_review.organisation_id=a.organisation_id AND latest_review.audit_id=a.id) "
        "AND r.effective_decision<>'NEEDS_REVIEW') "
        "AND (%s IN ('QA_ANALYST','ADMIN')) "
        "ORDER BY CASE a.decision WHEN 'NEEDS_REVIEW' THEN 0 WHEN 'FAIL' THEN 1 ELSE 2 END,c.created_at DESC,a.id "
        "LIMIT %s",
        (scope.organisation_id, scope.role, limit),
    ).fetchall()
    return [
        {"call_id": str(r[0]), "audit_id": str(r[1]), "machine_decision": r[2], "machine_score": r[3], "processing_state": r[4], "agent_id": r[5], "team_id": r[6], "created_at": r[7].isoformat(), "transcript_revision": r[8], "audit_revision": r[9]}
        for r in rows
    ]


def call_detail(connection, scope: Scope, call_id: str) -> dict[str, Any]:
    try:
        call_id = str(UUID(call_id))
    except (ValueError, TypeError, AttributeError):
        raise ReviewNotFound("Call not found") from None
    call = connection.execute(
        "SELECT id,organisation_id,agent_id,team_id,processing_state,language,created_at,transcript_revision,tombstoned_at "
        "FROM calls WHERE organisation_id=%s AND id=%s",
        (scope.organisation_id, call_id),
    ).fetchone()
    if call is None or call[8] is not None or not can_access(scope, str(call[1]), call[2], call[3]):
        raise ReviewNotFound("Call not found")
    audit = connection.execute(
        "SELECT id,revision,transcript_revision,model_artifact,inference_runtime,prompt_version,prompt_hash,rubric_version,rubric_hash,policy_provenance,dimensions_json,overall_score,decision,review_reason,coaching_narrative,highlights,improvement_areas,pass_threshold "
        "FROM audits WHERE organisation_id=%s AND call_id=%s ORDER BY revision DESC LIMIT 1",
        (scope.organisation_id, call_id),
    ).fetchone()
    transcript = connection.execute(
        "SELECT id,role,start_ms,end_ms,text_redacted,is_final FROM transcript_utterances "
        "WHERE organisation_id=%s AND call_id=%s AND revision=%s ORDER BY start_ms,id",
        (scope.organisation_id, call_id, call[7]),
    ).fetchall()
    findings = connection.execute(
        "SELECT rule_id,ruleset_version,ruleset_hash,status,severity,evidence_ids,deadline_ms,remediation "
        "FROM findings WHERE organisation_id=%s AND call_id=%s AND transcript_revision=%s ORDER BY rule_id",
        (scope.organisation_id, call_id, call[7]),
    ).fetchall()
    disposition = connection.execute(
        "SELECT revision,transcript_revision,config_id,config_version,config_hash,status,code,parent_code,requires_review,review_reason "
        "FROM dispositions WHERE organisation_id=%s AND call_id=%s AND transcript_revision=%s ORDER BY revision DESC LIMIT 1",
        (scope.organisation_id, call_id, call[7]),
    ).fetchone()
    # Advisory only (AGENTS.md): summary fields only, never the per-utterance signals.
    sentiment = connection.execute(
        "SELECT revision,transcript_revision,status,model_artifact,adapter_version,alert_offsets_ms,trend_json,failed_utterance_count,jsonb_array_length(signals_json) "
        "FROM call_sentiments WHERE organisation_id=%s AND call_id=%s AND transcript_revision=%s ORDER BY revision DESC LIMIT 1",
        (scope.organisation_id, call_id, call[7]),
    ).fetchone()
    reviews = []
    superseded = True
    if audit is not None:
        superseded = audit[2] != call[7] or audit[1] != connection.execute(
            "SELECT max(revision) FROM audits WHERE organisation_id=%s AND call_id=%s", (scope.organisation_id, call_id)
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT id,version,reviewer_id,action,changed_scores_json,effective_scores_json,effective_score,effective_decision,reason,created_at "
            "FROM reviews WHERE organisation_id=%s AND audit_id=%s ORDER BY version",
            (scope.organisation_id, audit[0]),
        ).fetchall()
        reviews = [{"id": str(r[0]), "version": r[1], "reviewer_id": r[2], "action": r[3], "changed_scores": r[4], "effective_scores": r[5], "effective_score": r[6], "effective_decision": r[7], "reason": r[8], "created_at": r[9].isoformat()} for r in rows]
    return {
        "call": {"id": str(call[0]), "agent_id": call[2], "team_id": call[3], "processing_state": call[4], "language": call[5], "created_at": call[6].isoformat(), "transcript_revision": call[7]},
        "audit": None if audit is None else {"id": str(audit[0]), "revision": audit[1], "transcript_revision": audit[2], "model_artifact": audit[3], "inference_runtime": audit[4], "prompt_version": audit[5], "prompt_hash": audit[6].strip(), "rubric_version": audit[7], "rubric_hash": audit[8].strip(), "policy_provenance": audit[9], "dimensions": audit[10], "machine_score": audit[11], "machine_decision": audit[12], "review_reason": audit[13], "coaching_narrative": audit[14], "highlights": audit[15], "improvement_areas": audit[16], "pass_threshold": audit[17], "superseded": superseded},
        "transcript": [{"id": r[0], "role": r[1], "start_ms": r[2], "end_ms": r[3], "text_redacted": r[4], "is_final": r[5]} for r in transcript],
        "findings": [{"rule_id": r[0], "ruleset_version": r[1], "ruleset_hash": r[2].strip(), "status": r[3], "severity": r[4], "evidence_ids": r[5], "deadline_ms": r[6], "remediation": r[7]} for r in findings],
        "disposition": None if disposition is None else {"revision": disposition[0], "transcript_revision": disposition[1], "config_id": disposition[2], "config_version": disposition[3], "config_hash": disposition[4].strip(), "status": disposition[5], "code": disposition[6], "parent_code": disposition[7], "requires_review": disposition[8], "review_reason": disposition[9]},
        "sentiment": None if sentiment is None else {"revision": sentiment[0], "transcript_revision": sentiment[1], "status": sentiment[2], "model_artifact": sentiment[3], "adapter_version": sentiment[4], "alert_offsets_ms": sentiment[5], "trend": sentiment[6], "failed_utterance_count": sentiment[7], "signal_count": sentiment[8]},
        "reviews": reviews,
        "current_review_version": reviews[-1]["version"] if reviews else 0,
    }
