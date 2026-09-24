"""Tenant and team scoped Exotel session lifecycle records."""

from datetime import datetime, timezone

from app.auth import Scope

_LIVE_ROLES = frozenset({"AGENT", "TEAM_LEADER", "QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"})


class LiveCallsForbidden(PermissionError):
    pass


def activate_exotel_session(connection, organisation_id: str, integration_id: str, call_key: str,
                            agent_ref: str, agent_id: str, team_id: str) -> int:
    """Lock the authenticated integration and current mapping while advancing generation."""
    with connection.transaction():
        mapping = connection.execute(
            "SELECT m.agent_id,m.team_id FROM exotel_integrations i "
            "JOIN exotel_agent_mappings m ON m.organisation_id=i.organisation_id AND m.integration_id=i.id "
            "WHERE i.organisation_id=%s AND i.id=%s AND i.is_active AND m.agent_ref=%s FOR UPDATE OF i,m",
            (organisation_id, integration_id, agent_ref),
        ).fetchone()
        memberships = connection.execute(
            "SELECT team_id FROM identity_memberships WHERE organisation_id=%s AND user_id=%s "
            "AND role='AGENT' AND is_active ORDER BY team_id",
            (organisation_id, agent_id),
        ).fetchall()
        if mapping != (agent_id, team_id) or memberships != [(team_id,)]:
            raise ValueError("Exotel mapping is no longer active")
        row = connection.execute(
            "INSERT INTO exotel_sessions(organisation_id,integration_id,call_key,generation,state,agent_id,team_id,"
            "started_at,draining_at,ended_at,incomplete_at,updated_at,last_activity_at,live_transcription_state,live_transcript_truncated) "
            "SELECT %s,%s,%s,1,'LIVE',%s,%s,now(),NULL,NULL,NULL,now(),now(),'DISABLED',false "
            "WHERE NOT EXISTS (SELECT 1 FROM calls c WHERE c.organisation_id=%s::uuid "
            "AND c.external_ref='exotel:'||%s AND c.tombstoned_at IS NOT NULL) "
            "ON CONFLICT (organisation_id,integration_id,call_key) DO UPDATE SET "
            "generation=exotel_sessions.generation+1,state='LIVE',agent_id=EXCLUDED.agent_id,team_id=EXCLUDED.team_id,"
            "started_at=now(),draining_at=NULL,ended_at=NULL,incomplete_at=NULL,updated_at=now(),last_activity_at=now(),"
            "live_transcription_state='DISABLED',live_transcript_truncated=false "
            "WHERE exotel_sessions.call_content_purged_at IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM calls c WHERE c.organisation_id=EXCLUDED.organisation_id "
            "AND c.external_ref='exotel:'||EXCLUDED.call_key AND c.tombstoned_at IS NOT NULL) "
            "RETURNING generation",
            (organisation_id, integration_id, call_key, agent_id, team_id, organisation_id, call_key),
        ).fetchone()
        if row is None:
            raise ValueError("Exotel call identity was tombstoned or purged")
    return int(row[0])


def update_exotel_session(connection, organisation_id: str, integration_id: str, call_key: str,
                          generation: int, state: str, *, activity: bool = False) -> bool:
    if state not in {"LIVE", "DRAINING", "ENDED", "INCOMPLETE"}:
        raise ValueError("invalid Exotel session state")
    if state == "LIVE" and not activity or state != "LIVE" and activity:
        raise ValueError("LIVE is reserved for activity updates")
    allowed_from = {"LIVE": "'LIVE'", "DRAINING": "'LIVE'", "ENDED": "'DRAINING'", "INCOMPLETE": "'LIVE','DRAINING'"}[state]
    timestamp = {"DRAINING": "draining_at", "ENDED": "ended_at", "INCOMPLETE": "incomplete_at"}.get(state)
    activity_sql = ",last_activity_at=now()" if activity else ""
    timestamp_sql = f",{timestamp}=now()" if timestamp else ""
    row = connection.execute(
        "UPDATE exotel_sessions SET state=%s,updated_at=now()" + timestamp_sql + activity_sql +
        " WHERE organisation_id=%s AND integration_id=%s AND call_key=%s AND generation=%s "
        f"AND state IN ({allowed_from}) RETURNING generation",
        (state, organisation_id, integration_id, call_key, generation),
    ).fetchone()
    return row is not None


def recover_stale_exotel_sessions(connection, organisation_id: str) -> int:
    """Bound orphaned LIVE sessions and allow a long but finite recording drain."""
    # ponytail: recover at most 200 per snapshot; later snapshots drain larger backlogs.
    return connection.execute(
        "WITH stale AS (SELECT organisation_id,integration_id,call_key FROM exotel_sessions "
        "WHERE organisation_id=%s::uuid AND ((state='LIVE' AND last_activity_at<now()-interval '90 seconds') "
        "OR (state='DRAINING' AND draining_at<now()-interval '2 hours 1 minute')) "
        "ORDER BY updated_at LIMIT 200 FOR UPDATE SKIP LOCKED) "
        "UPDATE exotel_sessions s SET state='INCOMPLETE',incomplete_at=now(),updated_at=now() FROM stale "
        "WHERE s.organisation_id=stale.organisation_id AND s.integration_id=stale.integration_id AND s.call_key=stale.call_key",
        (organisation_id,),
    ).rowcount


def recording_intake_committed(connection, organisation_id: str, integration_id: str, call_key: str,
                               generation: int, external_ref: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM calls c JOIN exotel_sessions s ON s.organisation_id=c.organisation_id "
        "WHERE c.organisation_id=%s::uuid AND c.external_ref=%s AND s.integration_id=%s "
        "AND s.call_key=%s AND s.generation=%s AND s.state='ENDED' LIMIT 1",
        (organisation_id, external_ref, integration_id, call_key, generation),
    ).fetchone() is not None


def read_live_calls(connection, scope: Scope) -> list[dict]:
    if scope.role not in _LIVE_ROLES:
        raise LiveCallsForbidden("role cannot read live calls")
    predicate = ""
    parameters: list[object] = [scope.organisation_id]
    if scope.role == "AGENT":
        predicate = " AND agent_id=%s"
        parameters.append(scope.user_id)
    elif scope.role == "TEAM_LEADER":
        if not scope.team_ids:
            return []
        predicate = " AND team_id=ANY(%s)"
        parameters.append(sorted(scope.team_ids))
    recover_stale_exotel_sessions(connection, scope.organisation_id)
    rows = connection.execute(
        "SELECT call_key,agent_id,team_id,state,started_at,draining_at,ended_at,incomplete_at,updated_at,last_activity_at,generation "
        "FROM exotel_sessions WHERE organisation_id=%s::uuid" + predicate +
        " AND state<>'UNKNOWN' AND agent_id IS NOT NULL AND team_id IS NOT NULL "
        " AND call_content_purged_at IS NULL AND NOT EXISTS (SELECT 1 FROM calls c "
        "WHERE c.organisation_id=exotel_sessions.organisation_id AND c.external_ref='exotel:'||exotel_sessions.call_key "
        "AND c.tombstoned_at IS NOT NULL) "
        " AND (state IN ('LIVE','DRAINING') OR updated_at >= now()-interval '15 minutes') "
        "ORDER BY updated_at DESC LIMIT 200",
        tuple(parameters),
    ).fetchall()
    now = datetime.now(timezone.utc)
    result = []
    for row in rows:
        activity = row[9]
        if activity.tzinfo is None:
            activity = activity.replace(tzinfo=timezone.utc)
        updated = row[8]
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        result.append({
            "call_key": row[0].strip(), "agent_id": row[1], "team_id": row[2], "state": row[3],
            "started_at": row[4].astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "draining_at": row[5].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if row[5] else None,
            "ended_at": row[6].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if row[6] else None,
            "incomplete_at": row[7].astimezone(timezone.utc).isoformat().replace("+00:00", "Z") if row[7] else None,
            "updated_at": updated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "stale": row[3] in {"LIVE", "DRAINING"} and (now - activity).total_seconds() > 45,
            "generation": int(row[10]),
        })
    return result
