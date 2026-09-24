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
            "started_at,draining_at,ended_at,incomplete_at,updated_at,last_activity_at) "
            "VALUES (%s,%s,%s,1,'LIVE',%s,%s,now(),NULL,NULL,NULL,now(),now()) "
            "ON CONFLICT (organisation_id,integration_id,call_key) DO UPDATE SET "
            "generation=exotel_sessions.generation+1,state='LIVE',agent_id=EXCLUDED.agent_id,team_id=EXCLUDED.team_id,"
            "started_at=now(),draining_at=NULL,ended_at=NULL,incomplete_at=NULL,updated_at=now(),last_activity_at=now() "
            "RETURNING generation",
            (organisation_id, integration_id, call_key, agent_id, team_id),
        ).fetchone()
    return int(row[0])


def update_exotel_session(connection, organisation_id: str, integration_id: str, call_key: str,
                          generation: int, state: str, *, activity: bool = False) -> bool:
    if state not in {"LIVE", "DRAINING", "ENDED", "INCOMPLETE"}:
        raise ValueError("invalid Exotel session state")
    timestamp = {"DRAINING": "draining_at", "ENDED": "ended_at", "INCOMPLETE": "incomplete_at"}.get(state)
    activity_sql = ",last_activity_at=now()" if activity else ""
    timestamp_sql = f",{timestamp}=now()" if timestamp else ""
    row = connection.execute(
        "UPDATE exotel_sessions SET state=%s,updated_at=now()" + timestamp_sql + activity_sql +
        " WHERE organisation_id=%s AND integration_id=%s AND call_key=%s AND generation=%s "
        "AND state IN ('LIVE','DRAINING') RETURNING generation",
        (state, organisation_id, integration_id, call_key, generation),
    ).fetchone()
    return row is not None


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
    rows = connection.execute(
        "SELECT call_key,agent_id,team_id,state,started_at,draining_at,ended_at,incomplete_at,updated_at,last_activity_at "
        "FROM exotel_sessions WHERE organisation_id=%s::uuid" + predicate +
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
        })
    return result
