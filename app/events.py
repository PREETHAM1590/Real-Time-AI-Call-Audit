"""Tenant-local durable events for safe post-call state updates."""

import json
from datetime import datetime, timezone

from app.auth import Scope, can_access

MAX_EVENT_SEQUENCE = 9_223_372_036_854_775_807
MAX_EVENT_BATCH = 100
# ponytail: keep 10k tenant events; older cursors reload the current snapshot.
MAX_EVENT_HISTORY = 10_000
_CALL_STATES = frozenset({"QUEUED", "TRANSCRIBING", "ANALYSING", "AUDITING", "READY", "RETRY_WAIT", "NEEDS_REVIEW", "FAILED", "DELETING", "DELETED"})
_READ_ROLES = frozenset({"AGENT", "TEAM_LEADER", "QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"})


class EventCursorError(ValueError):
    """The supplied resume cursor is invalid or ahead of the tenant stream."""


class EventCursorExpired(EventCursorError):
    def __init__(self, *, latest_sequence: int, oldest_sequence: int):
        super().__init__("event cursor has expired")
        self.latest_sequence = latest_sequence
        self.oldest_sequence = oldest_sequence


class EventForbidden(PermissionError):
    """The identity role cannot read event streams."""


def parse_last_event_id(value: str | None) -> int:
    if value is None:
        return 0
    if not isinstance(value, str) or not value or len(value) > 19 or not value.isascii() or not value.isdecimal():
        raise EventCursorError("invalid event cursor")
    cursor = int(value)
    if cursor > MAX_EVENT_SEQUENCE:
        raise EventCursorError("invalid event cursor")
    return cursor


def append_call_updated(connection, organisation_id: str, call_id: str, processing_state: str, transcript_revision: int) -> int:
    """Append a minimal state snapshot inside the caller's transaction."""
    if processing_state not in _CALL_STATES:
        raise ValueError("unsupported call state for event")
    if isinstance(transcript_revision, bool) or not isinstance(transcript_revision, int) or not 0 <= transcript_revision <= 2_147_483_647:
        raise ValueError("invalid transcript revision for event")
    row = connection.execute(
        "INSERT INTO event_counters(organisation_id,last_sequence,oldest_sequence) VALUES (%s,1,1) "
        "ON CONFLICT (organisation_id) DO UPDATE SET last_sequence=event_counters.last_sequence+1 "
        "RETURNING last_sequence",
        (organisation_id,),
    ).fetchone()
    sequence = int(row[0])
    payload = {"processing_state": processing_state, "transcript_revision": transcript_revision}
    connection.execute(
        "INSERT INTO events(organisation_id,sequence,call_id,type,schema_version,payload) "
        "VALUES (%s,%s,%s,'call.updated',1,%s::jsonb)",
        (organisation_id, sequence, call_id, json.dumps(payload, separators=(",", ":"))),
    )
    oldest = int(connection.execute(
        "SELECT oldest_sequence FROM event_counters WHERE organisation_id=%s FOR UPDATE",
        (organisation_id,),
    ).fetchone()[0])
    cutoff = connection.execute(
        "SELECT sequence FROM events WHERE organisation_id=%s ORDER BY sequence DESC OFFSET %s LIMIT 1",
        (organisation_id, MAX_EVENT_HISTORY - 1),
    ).fetchone()
    if cutoff is not None:
        oldest = max(oldest, int(cutoff[0]))
    connection.execute("DELETE FROM events WHERE organisation_id=%s AND sequence<%s", (organisation_id, oldest))
    connection.execute(
        "UPDATE event_counters SET oldest_sequence=%s WHERE organisation_id=%s AND oldest_sequence<%s",
        (oldest, organisation_id, oldest),
    )
    return sequence


def purge_call_events(connection, organisation_id: str, call_id: str) -> int:
    """Remove a purged call's refresh history and expire cursors that could contain it."""
    counter = connection.execute(
        "SELECT oldest_sequence FROM event_counters WHERE organisation_id=%s::uuid FOR UPDATE",
        (organisation_id,),
    ).fetchone()
    if counter is None:
        return 0
    latest_removed = connection.execute(
        "SELECT max(sequence) FROM events WHERE organisation_id=%s::uuid AND call_id=%s::uuid",
        (organisation_id, call_id),
    ).fetchone()[0]
    if latest_removed is None:
        return 0
    deleted = connection.execute(
        "DELETE FROM events WHERE organisation_id=%s::uuid AND call_id=%s::uuid",
        (organisation_id, call_id),
    ).rowcount
    oldest = max(int(counter[0]), int(latest_removed) + 1)
    connection.execute("DELETE FROM events WHERE organisation_id=%s::uuid AND sequence<%s", (organisation_id, oldest))
    connection.execute(
        "UPDATE event_counters SET oldest_sequence=%s WHERE organisation_id=%s::uuid AND oldest_sequence<%s",
        (oldest, organisation_id, oldest),
    )
    return deleted


def read_events(connection, scope: Scope, after_sequence: int, limit: int = 100) -> list[dict]:
    """Read a bounded, tenant/team/agent-authorized batch after a durable cursor."""
    if isinstance(after_sequence, bool) or not isinstance(after_sequence, int) or not 0 <= after_sequence <= MAX_EVENT_SEQUENCE:
        raise EventCursorError("invalid event cursor")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_EVENT_BATCH:
        raise ValueError("event batch limit must be between 1 and 100")
    if scope.role not in _READ_ROLES:
        raise EventForbidden("role cannot read call events")

    counter = connection.execute(
        "SELECT last_sequence,oldest_sequence FROM event_counters WHERE organisation_id=%s::uuid",
        (scope.organisation_id,),
    ).fetchone()
    if counter is None:
        if after_sequence > 0:
            raise EventCursorError("event cursor is ahead of an empty stream")
        return []
    latest_sequence, oldest_sequence = int(counter[0]), int(counter[1])
    if after_sequence > latest_sequence:
        raise EventCursorError("event cursor is ahead of the stream")
    if after_sequence < oldest_sequence - 1:
        raise EventCursorExpired(latest_sequence=latest_sequence, oldest_sequence=oldest_sequence)

    access_predicate = ""
    parameters: list[object] = [scope.organisation_id, after_sequence]
    if scope.role == "AGENT":
        access_predicate = "AND c.agent_id=%s"
        parameters.append(scope.user_id)
    elif scope.role == "TEAM_LEADER":
        if not scope.team_ids:
            return []
        access_predicate = "AND c.team_id=ANY(%s)"
        parameters.append(sorted(scope.team_ids))
    parameters.append(limit)
    rows = connection.execute(
        "SELECT e.sequence,e.schema_version,e.call_id::text,e.type,e.occurred_at,e.payload,"
        "c.organisation_id::text,c.agent_id,c.team_id,c.tombstoned_at "
        "FROM events e JOIN calls c ON c.organisation_id=e.organisation_id AND c.id=e.call_id "
        "WHERE e.organisation_id=%s::uuid AND e.sequence>%s "
        + access_predicate
        + " AND c.tombstoned_at IS NULL ORDER BY e.sequence LIMIT %s",
        tuple(parameters),
    ).fetchall()

    result = []
    for row in rows:
        if not can_access(scope, row[6], row[7], row[8]):
            continue
        occurred_at = row[4]
        if occurred_at.tzinfo is None:
            occurred_at = occurred_at.replace(tzinfo=timezone.utc)
        envelope = {
            "sequence": int(row[0]),
            "schema_version": int(row[1]),
            "call_id": row[2],
            "type": row[3],
            "occurred_at": occurred_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "payload": row[5],
        }
        result.append(envelope)
    return result


def reset_required_event(sequence: int) -> dict:
    return {
        "sequence": sequence,
        "schema_version": 1,
        "call_id": None,
        "type": "reset_required",
        "occurred_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload": {"reason": "cursor_expired"},
    }


def encode_sse(envelope: dict) -> str:
    data = json.dumps(envelope, separators=(",", ":"), ensure_ascii=False)
    return f"id: {envelope['sequence']}\nevent: {envelope['type']}\ndata: {data}\n\n"
