"""Short-lived, redacted provisional transcript snapshots for live sessions."""

from collections.abc import Callable, Sequence

from app.auth import Scope, can_access
from app.live_calls import LiveCallsForbidden
from app.privacy import redact_text

MAX_LIVE_UTTERANCES = 100
MAX_LIVE_UTTERANCE_CHARS = 1600
MAX_LIVE_SESSION_UTTERANCES = 500
LIVE_TRANSCRIPT_TTL_MINUTES = 15
LIVE_TRANSCRIPT_SWEEP_BATCH = 1000
_ROLES = frozenset({"AGENT", "CUSTOMER", "UNKNOWN", "IVR"})
_READ_ROLES = frozenset({"AGENT", "TEAM_LEADER", "QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"})


class LiveTranscriptUnavailable(LookupError):
    """Live transcript is absent, expired, tombstoned, or outside current scope."""


def _validated_utterances(items: Sequence[dict], redactor: Callable[[str], str]) -> list[dict]:
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)) or not 1 <= len(items) <= MAX_LIVE_UTTERANCES:
        raise ValueError("invalid live utterance batch")
    output = []
    seen = set()
    total_chars = 0
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("invalid live utterance")
        utterance_id, role = item.get("id"), item.get("role")
        start_ms, end_ms, text = item.get("start_ms"), item.get("end_ms"), item.get("text")
        if (not isinstance(utterance_id, str) or len(utterance_id) != 32
                or any(char not in "0123456789abcdef" for char in utterance_id)
                or utterance_id in seen
                or not isinstance(role, str) or role not in _ROLES or type(start_ms) is not int or type(end_ms) is not int
                or start_ms < 0 or end_ms < start_ms or not isinstance(text, str)
                or not text.strip() or len(text) > MAX_LIVE_UTTERANCE_CHARS):
            raise ValueError("invalid live utterance")
        seen.add(utterance_id)
        total_chars += len(text)
        if total_chars > MAX_LIVE_UTTERANCES * MAX_LIVE_UTTERANCE_CHARS:
            raise ValueError("live utterance batch is too large")
        redacted = redactor(text)
        if not isinstance(redacted, str) or len(redacted) > MAX_LIVE_UTTERANCE_CHARS:
            raise ValueError("invalid redacted live utterance")
        output.append({"id": utterance_id, "role": role, "start_ms": start_ms,
                       "end_ms": end_ms, "text_redacted": redacted})
    return output


def store_live_utterances(connection, organisation_id: str, integration_id: str, call_key: str,
                          generation: int, items: Sequence[dict], *, redactor: Callable[[str], str] = redact_text) -> int:
    """Replace provisional IDs for the current session generation; retain redacted text for 15 minutes."""
    if (not isinstance(call_key, str) or len(call_key) != 64 or any(c not in "0123456789abcdef" for c in call_key)
            or type(generation) is not int or generation < 1):
        raise ValueError("invalid live session reference")
    utterances = _validated_utterances(items, redactor)
    with connection.transaction():
        session = connection.execute(
            "SELECT s.state,s.call_content_purged_at FROM exotel_sessions s "
            "WHERE s.organisation_id=%s::uuid AND s.integration_id=%s::uuid AND s.call_key=%s "
            "AND s.generation=%s AND s.state IN ('LIVE','DRAINING') FOR UPDATE",
            (organisation_id, integration_id, call_key, generation),
        ).fetchone()
        if session is None or session[0] not in {"LIVE", "DRAINING"} or session[1] is not None:
            raise LiveTranscriptUnavailable("live transcript unavailable")
        call = connection.execute(
            "SELECT tombstoned_at FROM calls WHERE organisation_id=%s::uuid AND external_ref='exotel:'||%s FOR UPDATE",
            (organisation_id, call_key),
        ).fetchone()
        if call is not None and call[0] is not None:
            raise LiveTranscriptUnavailable("live transcript unavailable")
        connection.execute(
            "DELETE FROM live_transcript_utterances WHERE organisation_id=%s::uuid AND integration_id=%s::uuid "
            "AND call_key=%s AND generation=%s AND expires_at<=now()",
            (organisation_id, integration_id, call_key, generation),
        )
        count = connection.execute(
            "SELECT count(*) FROM live_transcript_utterances WHERE organisation_id=%s::uuid AND integration_id=%s::uuid "
            "AND call_key=%s AND generation=%s AND expires_at>now()",
            (organisation_id, integration_id, call_key, generation),
        ).fetchone()[0]
        ids = {item["id"] for item in utterances}
        existing = connection.execute(
            "SELECT utterance_id FROM live_transcript_utterances WHERE organisation_id=%s::uuid AND integration_id=%s::uuid "
            "AND call_key=%s AND generation=%s AND expires_at>now()",
            (organisation_id, integration_id, call_key, generation),
        ).fetchall()
        new_count = len(ids - {row[0] for row in existing})
        for item in utterances:
            connection.execute(
                "INSERT INTO live_transcript_utterances(organisation_id,integration_id,call_key,generation,utterance_id,"
                "start_ms,end_ms,role,text_redacted,is_final,expires_at) "
                "VALUES (%s::uuid,%s::uuid,%s,%s,%s,%s,%s,%s,%s,false,now()+make_interval(mins=>%s)) "
                "ON CONFLICT (organisation_id,integration_id,call_key,generation,utterance_id) DO UPDATE SET "
                "start_ms=EXCLUDED.start_ms,end_ms=EXCLUDED.end_ms,role=EXCLUDED.role,text_redacted=EXCLUDED.text_redacted,"
                "expires_at=EXCLUDED.expires_at",
                (organisation_id, integration_id, call_key, generation, item["id"], item["start_ms"],
                 item["end_ms"], item["role"], item["text_redacted"], LIVE_TRANSCRIPT_TTL_MINUTES),
            )
        total = int(count) + new_count
        if total > MAX_LIVE_SESSION_UTTERANCES:
            connection.execute(
                "WITH excess AS (SELECT utterance_id FROM live_transcript_utterances "
                "WHERE organisation_id=%s::uuid AND integration_id=%s::uuid AND call_key=%s AND generation=%s "
                "AND expires_at>now() ORDER BY start_ms DESC,utterance_id DESC OFFSET %s) "
                "DELETE FROM live_transcript_utterances u USING excess e WHERE u.organisation_id=%s::uuid "
                "AND u.integration_id=%s::uuid AND u.call_key=%s AND u.generation=%s AND u.utterance_id=e.utterance_id",
                (organisation_id, integration_id, call_key, generation, MAX_LIVE_SESSION_UTTERANCES,
                 organisation_id, integration_id, call_key, generation),
            )
            connection.execute(
                "UPDATE exotel_sessions SET live_transcript_truncated=true WHERE organisation_id=%s::uuid "
                "AND integration_id=%s::uuid AND call_key=%s AND generation=%s AND state IN ('LIVE','DRAINING')",
                (organisation_id, integration_id, call_key, generation),
            )
        connection.execute(
            "UPDATE exotel_sessions SET live_transcription_state=CASE WHEN live_transcription_state='DEGRADED' "
            "THEN 'DEGRADED' ELSE 'LIVE' END WHERE organisation_id=%s::uuid AND integration_id=%s::uuid "
            "AND call_key=%s AND generation=%s AND state IN ('LIVE','DRAINING')",
            (organisation_id, integration_id, call_key, generation),
        )
    return len(utterances)


def update_live_transcription_state(connection, organisation_id: str, integration_id: str, call_key: str,
                                    generation: int, state: str) -> bool:
    if state not in {"DISABLED", "EMPTY", "DEGRADED"} or type(generation) is not int or generation < 1:
        raise ValueError("invalid live transcription state")
    row = connection.execute(
        "UPDATE exotel_sessions SET live_transcription_state=%s WHERE organisation_id=%s::uuid "
        "AND integration_id=%s::uuid AND call_key=%s AND generation=%s AND state IN ('LIVE','DRAINING') "
        "AND call_content_purged_at IS NULL AND NOT EXISTS (SELECT 1 FROM calls c "
        "WHERE c.organisation_id=exotel_sessions.organisation_id AND c.external_ref='exotel:'||exotel_sessions.call_key "
        "AND c.tombstoned_at IS NOT NULL) AND (%s<>'DISABLED' OR live_transcription_state<>'LIVE') "
        "RETURNING generation",
        (state, organisation_id, integration_id, call_key, generation, state),
    ).fetchone()
    return row is not None


def read_live_utterances(connection, scope: Scope, call_key: str, generation: int) -> dict:
    if scope.role not in _READ_ROLES:
        raise LiveCallsForbidden("role cannot read live transcripts")
    if (not isinstance(call_key, str) or len(call_key) != 64 or any(c not in "0123456789abcdef" for c in call_key)
            or type(generation) is not int or generation < 1):
        raise ValueError("invalid live session reference")
    predicate = ""
    parameters: list[object] = [scope.organisation_id, call_key, generation]
    if scope.role == "AGENT":
        predicate = " AND s.agent_id=%s"
        parameters.append(scope.user_id)
    elif scope.role == "TEAM_LEADER":
        if not scope.team_ids:
            raise LiveTranscriptUnavailable("live transcript unavailable")
        predicate = " AND s.team_id=ANY(%s)"
        parameters.append(sorted(scope.team_ids))
    rows = connection.execute(
        "SELECT u.utterance_id,u.role,u.start_ms,u.end_ms,u.text_redacted,s.agent_id,s.team_id,"
        "s.live_transcription_state,s.live_transcript_truncated "
        "FROM exotel_sessions s "
        "LEFT JOIN calls c ON c.organisation_id=s.organisation_id AND c.external_ref='exotel:'||s.call_key "
        "LEFT JOIN live_transcript_utterances u ON s.organisation_id=u.organisation_id "
        "AND s.integration_id=u.integration_id AND s.call_key=u.call_key AND s.generation=u.generation "
        "AND u.expires_at>now() "
        "WHERE s.organisation_id=%s::uuid AND s.call_key=%s AND s.generation=%s "
        "AND s.state IN ('LIVE','DRAINING','ENDED','INCOMPLETE') "
        "AND s.call_content_purged_at IS NULL AND c.tombstoned_at IS NULL"
        + predicate + " ORDER BY u.start_ms,u.utterance_id LIMIT 500",
        tuple(parameters),
    ).fetchall()
    if not rows:
        raise LiveTranscriptUnavailable("live transcript unavailable")
    for row in rows:
        if not can_access(scope, scope.organisation_id, row[5], row[6]):
            raise LiveTranscriptUnavailable("live transcript unavailable")
    return {
        "status": rows[0][7], "truncated": bool(rows[0][8]),
        "items": [{"id": row[0], "role": row[1], "start_ms": row[2], "end_ms": row[3],
                   "text_redacted": row[4], "is_final": False} for row in rows if row[0] is not None],
    }


def purge_expired_live_utterances(connection, *, limit: int = LIVE_TRANSCRIPT_SWEEP_BATCH) -> int:
    if type(limit) is not int or not 1 <= limit <= LIVE_TRANSCRIPT_SWEEP_BATCH:
        raise ValueError("invalid live transcript sweep batch")
    with connection.transaction():
        return connection.execute(
            "WITH expired AS (SELECT ctid FROM live_transcript_utterances WHERE expires_at<=now() "
            "ORDER BY expires_at LIMIT %s FOR UPDATE SKIP LOCKED) "
            "DELETE FROM live_transcript_utterances u USING expired e WHERE u.ctid=e.ctid",
            (limit,),
        ).rowcount
