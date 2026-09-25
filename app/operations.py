"""Tenant-scoped, bounded aggregate operational visibility."""

MAX_QUEUE_AGE_SECONDS = 31_536_000

# This module's own bounded stage allowlist for the per-stage dashboard breakdown.
# Kept separately from app.worker._KNOWN_STAGES (not imported, to avoid coupling
# this module's import graph to worker stage registration) -- update both when a
# stage is added. Any other stage value is folded into "UNKNOWN".
_KNOWN_STAGES = frozenset({"TRANSCRIBE", "ANALYSE", "POLICY", "AUDIT", "SENTIMENT"})


def operations_summary(connection, organisation_id: str) -> dict:
    """Return aggregate work state without identifiers or content.

    `incomplete_calls` counts visible calls not in READY, including failed and
    needs-review work. READY is not emitted by the current post-call pipeline.
    `pending_by_stage` breaks the same pending-job count down per stage (bounded
    to known stage names, unexpected values folded into "UNKNOWN"), still scoped
    to the caller's organisation only.
    """
    row = connection.execute(
        "WITH scoped_calls AS ("
        "SELECT organisation_id,id,processing_state FROM calls "
        "WHERE organisation_id=%s::uuid AND tombstoned_at IS NULL), "
        "pending AS ("
        "SELECT j.stage,j.created_at FROM jobs j JOIN scoped_calls c "
        "ON c.organisation_id=j.organisation_id AND c.id=j.call_id "
        "WHERE j.state IN ('QUEUED','RETRY_WAIT','WAITING_HANDLER')), "
        "by_stage AS ("
        "SELECT stage,count(*) AS pending,"
        "LEAST(%s,GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (now()-min(created_at))))))::bigint AS oldest_age "
        "FROM pending GROUP BY stage) "
        "SELECT "
        "(SELECT count(*) FROM pending), "
        "COALESCE((SELECT LEAST(%s,GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (now()-min(created_at))))))::bigint FROM pending),0), "
        "(SELECT count(*) FROM scoped_calls WHERE processing_state<>'READY'), "
        "COALESCE((SELECT jsonb_object_agg(stage,jsonb_build_object('pending',pending,'oldest_pending_age_seconds',oldest_age)) FROM by_stage),'{}'::jsonb)",
        (organisation_id, MAX_QUEUE_AGE_SECONDS, MAX_QUEUE_AGE_SECONDS),
    ).fetchone()
    if row is None:
        return {"pending_jobs": 0, "oldest_pending_age_seconds": 0, "incomplete_calls": 0, "pending_by_stage": {}}
    pending_jobs, oldest_age, incomplete_calls, by_stage_raw = row
    pending_by_stage: dict[str, dict[str, int]] = {}
    for stage, value in (by_stage_raw or {}).items():
        safe_stage = stage if stage in _KNOWN_STAGES else "UNKNOWN"
        bucket = pending_by_stage.setdefault(safe_stage, {"pending": 0, "oldest_pending_age_seconds": 0})
        bucket["pending"] += max(0, int(value.get("pending", 0)))
        bucket["oldest_pending_age_seconds"] = max(
            bucket["oldest_pending_age_seconds"],
            min(MAX_QUEUE_AGE_SECONDS, max(0, int(value.get("oldest_pending_age_seconds", 0)))),
        )
    return {
        "pending_jobs": max(0, int(pending_jobs)),
        "oldest_pending_age_seconds": min(MAX_QUEUE_AGE_SECONDS, max(0, int(oldest_age))),
        "incomplete_calls": max(0, int(incomplete_calls)),
        "pending_by_stage": pending_by_stage,
    }
