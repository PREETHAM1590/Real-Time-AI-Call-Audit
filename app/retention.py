"""Bounded tombstone-first lifecycle operations for retained call data."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

MAX_RETENTION_BATCH = 100
MAX_AUDIO_OBJECTS_PER_CALL = 16


class RetentionError(ValueError):
    """Invalid lifecycle inputs or unsafe state transitions."""


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("UTC-aware timestamps required")
    return value.astimezone(timezone.utc)


def due_for_deletion(expires_at: datetime, now: datetime, legal_hold: bool) -> bool:
    expires_utc, now_utc = _aware(expires_at), _aware(now)
    if not isinstance(legal_hold, bool):
        raise ValueError("legal_hold must be boolean")
    return not legal_hold and expires_utc <= now_utc


def _scope_ids(organisation_id: str, call_id: str) -> tuple[UUID, UUID]:
    try:
        return UUID(str(organisation_id)), UUID(str(call_id))
    except (ValueError, TypeError, AttributeError):
        raise RetentionError("invalid organisation or call identifier") from None


def _cancel_unclaimed(connection, organisation_id: UUID, call_id: UUID) -> int:
    return connection.execute(
        "UPDATE jobs SET state='FAILED',lease_token=NULL,lease_until=NULL,last_error_code='TOMBSTONED' "
        "WHERE organisation_id=%s AND call_id=%s AND state IN ('QUEUED','WAITING_HANDLER','RETRY_WAIT')",
        (organisation_id, call_id),
    ).rowcount


def request_call_deletion(connection, organisation_id: str, call_id: str, actor_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Tombstone immediately; workers and APIs must stop before physical cleanup."""
    org, call = _scope_ids(organisation_id, call_id)
    instant = _aware(now or datetime.now(timezone.utc))
    if not isinstance(actor_id, str) or not actor_id.strip() or len(actor_id) > 255:
        raise RetentionError("invalid deletion actor")
    with connection.transaction():
        row = connection.execute(
            "SELECT legal_hold,tombstoned_at,content_purged_at FROM calls WHERE organisation_id=%s AND id=%s FOR UPDATE",
            (org, call),
        ).fetchone()
        if row is None:
            return {"status": "NOT_FOUND"}
        if row[0]:
            return {"status": "HELD"}
        if row[1] is not None:
            return {"status": "ALREADY_TOMBSTONED" if row[2] is None else "ALREADY_PURGED"}
        connection.execute(
            "UPDATE calls SET tombstoned_at=%s,processing_state='DELETING',deletion_requested_at=%s,deletion_requested_by=%s "
            "WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL",
            (instant, instant, actor_id, org, call),
        )
        cancelled = _cancel_unclaimed(connection, org, call)
        connection.execute(
            "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome,details) "
            "VALUES (%s,%s,%s,'CALL_DELETION_REQUESTED','CALL',%s,'SUCCESS',%s::jsonb)",
            (org, uuid4(), actor_id, call, '{"source":"admin_request"}'),
        )
    return {"status": "TOMBSTONED", "cancelled_jobs": cancelled}


def tombstone_expired_calls(connection, *, now: datetime | None = None, limit: int = MAX_RETENTION_BATCH, organisation_id: str | None = None) -> int:
    """Tombstone at most `limit` expired, non-held calls; no content is deleted here."""
    instant = _aware(now or datetime.now(timezone.utc))
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RETENTION_BATCH:
        raise RetentionError(f"retention batch limit must be between 1 and {MAX_RETENTION_BATCH}")
    try:
        organisation = UUID(organisation_id) if organisation_id is not None else None
    except (ValueError, TypeError, AttributeError):
        raise RetentionError("invalid organisation identifier") from None
    with connection.transaction():
        rows = connection.execute(
            "WITH due AS (SELECT organisation_id,id FROM calls WHERE retention_expires_at<=%s AND NOT legal_hold "
            "AND tombstoned_at IS NULL AND (%s::uuid IS NULL OR organisation_id=%s) "
            "ORDER BY retention_expires_at,organisation_id,id LIMIT %s FOR UPDATE SKIP LOCKED) "
            "UPDATE calls c SET tombstoned_at=%s,processing_state='DELETING' FROM due "
            "WHERE c.organisation_id=due.organisation_id AND c.id=due.id RETURNING c.organisation_id,c.id",
            (instant, organisation, organisation, limit, instant),
        ).fetchall()
        for org, call in rows:
            _cancel_unclaimed(connection, org, call)
            connection.execute(
                "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome,details) "
                "VALUES (%s,%s,'retention-worker','CALL_DELETION_REQUESTED','CALL',%s,'SUCCESS','{\"source\":\"expiry\"}'::jsonb)",
                (org, uuid4(), call),
            )
    return len(rows)


def purge_call(connection, organisation_id: str, call_id: str, storage, *, now: datetime | None = None) -> dict[str, Any]:
    """Remove audio and mutable derivatives after tombstone, retaining immutable evidence explicitly."""
    org, call = _scope_ids(organisation_id, call_id)
    instant = _aware(now or datetime.now(timezone.utc))
    with connection.transaction():
        row = connection.execute(
            "SELECT tombstoned_at,legal_hold,retention_expires_at,deletion_requested_at,content_purged_at "
            "FROM calls WHERE organisation_id=%s AND id=%s FOR UPDATE",
            (org, call),
        ).fetchone()
        if row is None:
            return {"status": "NOT_FOUND"}
        if row[1]:
            return {"status": "HELD"}
        if row[4] is not None:
            return {"status": "ALREADY_PURGED"}
        if row[0] is None:
            return {"status": "NOT_TOMBSTONED"}
        explicitly_requested = row[3] is not None
        expired = row[2] is not None and due_for_deletion(row[2], instant, False)
        if not explicitly_requested and not expired:
            return {"status": "NOT_DUE"}

        object_rows = connection.execute(
            "SELECT private_key FROM audio_objects WHERE organisation_id=%s AND call_id=%s ORDER BY id LIMIT %s",
            (org, call, MAX_AUDIO_OBJECTS_PER_CALL + 1),
        ).fetchall()
        if len(object_rows) > MAX_AUDIO_OBJECTS_PER_CALL:
            raise RetentionError("audio object count exceeds per-call purge bound")
        counts = {}
        for table in ("audits", "reviews", "dispositions"):
            # Table identifiers are static constants, never user input.
            counts[table] = connection.execute(
                f"SELECT count(*) FROM {table} WHERE organisation_id=%s AND call_id=%s", (org, call)
            ).fetchone()[0]

        # Keep the call row locked from hold verification through object deletion,
        # so a concurrent hold cannot race past this decision point.
        for object_row in object_rows:
            storage.delete(object_row[0])
        connection.execute("DELETE FROM audio_objects WHERE organisation_id=%s AND call_id=%s", (org, call))
        connection.execute("DELETE FROM transcript_utterances WHERE organisation_id=%s AND call_id=%s", (org, call))
        connection.execute("DELETE FROM findings WHERE organisation_id=%s AND call_id=%s", (org, call))
        connection.execute("DELETE FROM jobs WHERE organisation_id=%s AND call_id=%s", (org, call))
        connection.execute(
            "UPDATE calls SET external_ref='deleted:'||id::text,idempotency_key='deleted:'||id::text,payload_sha256=repeat('0',64),"
            "agent_id='[deleted]',team_id='[deleted]',language='und',call_type='unknown',agent_connected_ms=NULL,"
            "tagged_intervals='[]'::jsonb,call_complete=false,timing_reliable=false,processing_state='DELETED',content_purged_at=%s "
            "WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NOT NULL AND legal_hold=false",
            (instant, org, call),
        )
        connection.execute(
            "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome,details) "
            "VALUES (%s,%s,'retention-worker','CALL_CONTENT_PURGED','CALL',%s,'SUCCESS',%s::jsonb)",
            (org, uuid4(), call, json.dumps({"retained_immutable_history": counts})),
        )
    retained_count = sum(counts.values())
    return {
        "status": "PARTIAL_IMMUTABLE_HISTORY" if retained_count else "CONTENT_PURGED",
        "audio_objects_removed": len(object_rows),
        "mutable_derivatives_removed": True,
        "immutable_records_retained": counts,
    }
