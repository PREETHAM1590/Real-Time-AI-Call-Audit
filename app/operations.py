"""Tenant-scoped, bounded aggregate operational visibility."""

MAX_QUEUE_AGE_SECONDS = 31_536_000


def operations_summary(connection, organisation_id: str) -> dict[str, int]:
    """Return aggregate work state without identifiers or content.

    `incomplete_calls` counts visible calls not in READY, including failed and
    needs-review work. READY is not emitted by the current post-call pipeline.
    """
    row = connection.execute(
        "WITH scoped_calls AS ("
        "SELECT organisation_id,id,processing_state FROM calls "
        "WHERE organisation_id=%s::uuid AND tombstoned_at IS NULL), "
        "pending AS ("
        "SELECT j.created_at FROM jobs j JOIN scoped_calls c "
        "ON c.organisation_id=j.organisation_id AND c.id=j.call_id "
        "WHERE j.state IN ('QUEUED','RETRY_WAIT','WAITING_HANDLER')) "
        "SELECT "
        "(SELECT count(*) FROM pending), "
        "COALESCE((SELECT LEAST(%s,GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (now()-min(created_at))))))::bigint FROM pending),0), "
        "(SELECT count(*) FROM scoped_calls WHERE processing_state<>'READY')",
        (organisation_id, MAX_QUEUE_AGE_SECONDS),
    ).fetchone()
    if row is None:
        return {"pending_jobs": 0, "oldest_pending_age_seconds": 0, "incomplete_calls": 0}
    pending_jobs, oldest_age, incomplete_calls = row
    return {
        "pending_jobs": max(0, int(pending_jobs)),
        "oldest_pending_age_seconds": min(MAX_QUEUE_AGE_SECONDS, max(0, int(oldest_age))),
        "incomplete_calls": max(0, int(incomplete_calls)),
    }
