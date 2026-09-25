"""Aggregate, no-identifier metrics: the worker stage counter and the /metrics exporter.

`worker_stage_counters` and the Prometheus exposition text built here carry only
bounded enum labels (stage/outcome/state) and counts/durations/ages -- never a
tenant, call, job or agent identifier, and never raw transcript or error text.
"""

import hmac
import logging
import os

from app.db import connect
from app.operations import MAX_QUEUE_AGE_SECONDS

_LOGGER = logging.getLogger(__name__)

METRICS_TOKEN_MIN_LENGTH = 32

# ponytail: one hour is far beyond any real stage attempt; bound what a single
# counter update can add so a corrupted caller value can never inflate the
# aggregate table without limit.
MAX_ATTEMPT_DURATION_MS = 3_600_000

# This exporter's own bounded label allowlists. Kept separately from
# app.worker._KNOWN_STAGES (not imported, to avoid coupling this module's import
# graph to worker stage registration) -- update both when a stage is added.
KNOWN_STAGES = frozenset({"TRANSCRIBE", "ANALYSE", "POLICY", "AUDIT"})
KNOWN_JOB_STATES = frozenset({"QUEUED", "RUNNING", "WAITING_HANDLER", "DONE", "RETRY_WAIT", "FAILED"})
KNOWN_CALL_STATES = frozenset({
    "QUEUED", "TRANSCRIBING", "ANALYSING", "AUDITING", "READY",
    "RETRY_WAIT", "NEEDS_REVIEW", "FAILED", "DELETING", "DELETED",
})
KNOWN_STAGE_OUTCOMES = frozenset({
    "WAITING_HANDLER", "LEASE_LOST", "COMMITTED", "STALE_COMMIT",
    "RETRY_HANDLED", "RETRY_HANDLER_FAILED", "UNKNOWN",
})
KNOWN_SESSION_STATES = frozenset({"UNKNOWN", "LIVE", "DRAINING", "ENDED", "INCOMPLETE"})

UNKNOWN_LABEL = "UNKNOWN"

_DATABASE_UP_HEADER = (
    "# HELP call_audit_database_up Whether the most recent metrics scrape's database queries succeeded (1) or failed (0).\n"
    "# TYPE call_audit_database_up gauge\n"
)


def record_stage_counter(stage: str, outcome: str, duration_ms: int) -> None:
    """Upsert one claimed job attempt's aggregate counter.

    Called with the already-sanitised stage/outcome/duration from
    `app.worker._log_stage_outcome`. This must never raise and must never affect
    job processing: any failure (missing table, unreachable database, timeout) is
    swallowed and logged at WARNING with no identifiers.
    """
    try:
        safe_duration = min(MAX_ATTEMPT_DURATION_MS, max(0, int(duration_ms)))
        with connect(timeout_seconds=2) as connection:
            connection.execute(
                "INSERT INTO worker_stage_counters(stage,outcome,attempts,total_duration_ms) VALUES (%s,%s,1,%s) "
                "ON CONFLICT (stage,outcome) DO UPDATE SET "
                "attempts=worker_stage_counters.attempts+1,"
                "total_duration_ms=worker_stage_counters.total_duration_ms+EXCLUDED.total_duration_ms,"
                "updated_at=now()",
                (stage, outcome, safe_duration),
            )
    except Exception:
        _LOGGER.warning("worker stage counter update failed", extra={"event_name": "metrics.stage_counter_failed"})


def metrics_token_from_env() -> str | None:
    """Return the configured metrics bearer token, or None when metrics are disabled.

    Disabled unless METRICS_TOKEN is set to at least 32 characters.
    """
    token = os.environ.get("METRICS_TOKEN")
    if not isinstance(token, str) or len(token) < METRICS_TOKEN_MIN_LENGTH:
        return None
    return token


def verify_metrics_bearer(authorization_header: str | None, expected_token: str) -> bool:
    """Constant-time check of an `Authorization: Bearer <token>` header. Never logs it."""
    if not isinstance(authorization_header, str):
        return False
    scheme, _, credential = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return False
    return hmac.compare_digest(credential, expected_token)


def _bounded(value, known: frozenset) -> str:
    return value if isinstance(value, str) and value in known else UNKNOWN_LABEL


def _escape_label_value(value) -> str:
    text = str(value)
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _metric_line(name: str, labels: dict, value) -> str:
    if not labels:
        return f"{name} {value}"
    formatted = ",".join(f'{key}="{_escape_label_value(label_value)}"' for key, label_value in labels.items())
    return f"{name}{{{formatted}}} {value}"


def _header(name: str, type_: str, help_text: str) -> str:
    return f"# HELP {name} {help_text}\n# TYPE {name} {type_}"


def _aggregate(rows, bucket_fns, *, combine) -> dict:
    aggregated: dict[tuple, object] = {}
    for row in rows:
        *label_values, measure = row
        key = tuple(fn(value) for fn, value in zip(bucket_fns, label_values))
        aggregated[key] = combine(aggregated.get(key), measure)
    return aggregated


def _sum(existing, value):
    return int(value) if existing is None else existing + int(value)


def _max_nonnegative(cap: int):
    def combine(existing, value):
        bounded = min(cap, max(0, int(value)))
        return bounded if existing is None else max(existing, bounded)
    return combine


def render_database_down_text() -> str:
    return _DATABASE_UP_HEADER + "call_audit_database_up 0\n"


def render_metrics_text(connection) -> str:
    """Render the full Prometheus 0.0.4 exposition text for one scrape.

    Runs every query against `connection` first; if any of them fails (missing
    table, statement timeout, connection error), the scrape still returns a
    complete, valid document -- just `call_audit_database_up 0` and nothing else,
    never a 500 and never a half-built series set.
    """
    try:
        job_rows = connection.execute("SELECT stage,state,count(*) FROM jobs GROUP BY stage,state").fetchall()
        pending_rows = connection.execute(
            "SELECT stage, LEAST(%s,GREATEST(0,FLOOR(EXTRACT(EPOCH FROM (now()-min(created_at))))))::bigint "
            "FROM jobs WHERE state IN ('QUEUED','RETRY_WAIT','WAITING_HANDLER') GROUP BY stage",
            (MAX_QUEUE_AGE_SECONDS,),
        ).fetchall()
        call_rows = connection.execute(
            "SELECT processing_state,count(*) FROM calls WHERE tombstoned_at IS NULL GROUP BY processing_state"
        ).fetchall()
        stage_counter_rows = connection.execute("SELECT stage,outcome,attempts,total_duration_ms FROM worker_stage_counters").fetchall()
        session_rows = connection.execute("SELECT state,count(*) FROM exotel_sessions GROUP BY state").fetchall()
        outbox_row = connection.execute("SELECT count(*) FROM events").fetchone()
    except Exception:
        _LOGGER.warning("metrics scrape query failed", extra={"event_name": "metrics.query_failed"})
        return render_database_down_text()

    lines: list[str] = []

    lines.append(_header("call_audit_jobs", "gauge", "Number of jobs by stage and lifecycle state, aggregated across all tenants."))
    jobs_by_stage_state = _aggregate(job_rows, (lambda s: _bounded(s, KNOWN_STAGES), lambda s: _bounded(s, KNOWN_JOB_STATES)), combine=_sum)
    for (stage, state), count in sorted(jobs_by_stage_state.items()):
        lines.append(_metric_line("call_audit_jobs", {"stage": stage, "state": state}, count))

    lines.append(_header(
        "call_audit_oldest_pending_job_age_seconds", "gauge",
        f"Age in seconds of the oldest pending (QUEUED/RETRY_WAIT/WAITING_HANDLER) job per stage, capped at {MAX_QUEUE_AGE_SECONDS} seconds.",
    ))
    oldest_by_stage = _aggregate(pending_rows, (lambda s: _bounded(s, KNOWN_STAGES),), combine=_max_nonnegative(MAX_QUEUE_AGE_SECONDS))
    for (stage,), age in sorted(oldest_by_stage.items()):
        lines.append(_metric_line("call_audit_oldest_pending_job_age_seconds", {"stage": stage}, age))

    lines.append(_header("call_audit_calls", "gauge", "Number of non-tombstoned calls by processing state, aggregated across all tenants."))
    calls_by_state = _aggregate(call_rows, (lambda s: _bounded(s, KNOWN_CALL_STATES),), combine=_sum)
    for (state,), count in sorted(calls_by_state.items()):
        lines.append(_metric_line("call_audit_calls", {"processing_state": state}, count))

    lines.append(_header("call_audit_worker_stage_attempts_total", "counter", "Total claimed worker job attempts by stage and outcome."))
    attempts_by_stage_outcome = _aggregate(
        ((stage, outcome, attempts) for stage, outcome, attempts, _duration in stage_counter_rows),
        (lambda s: _bounded(s, KNOWN_STAGES), lambda o: _bounded(o, KNOWN_STAGE_OUTCOMES)),
        combine=_sum,
    )
    for (stage, outcome), count in sorted(attempts_by_stage_outcome.items()):
        lines.append(_metric_line("call_audit_worker_stage_attempts_total", {"stage": stage, "outcome": outcome}, count))

    lines.append(_header("call_audit_worker_stage_duration_milliseconds_total", "counter", "Total elapsed milliseconds across claimed worker job attempts by stage and outcome."))
    duration_by_stage_outcome = _aggregate(
        ((stage, outcome, duration) for stage, outcome, _attempts, duration in stage_counter_rows),
        (lambda s: _bounded(s, KNOWN_STAGES), lambda o: _bounded(o, KNOWN_STAGE_OUTCOMES)),
        combine=_sum,
    )
    for (stage, outcome), total_duration_ms in sorted(duration_by_stage_outcome.items()):
        lines.append(_metric_line("call_audit_worker_stage_duration_milliseconds_total", {"stage": stage, "outcome": outcome}, total_duration_ms))

    lines.append(_header("call_audit_live_sessions", "gauge", "Number of Exotel live session rows by lifecycle state, aggregated across all tenants."))
    sessions_by_state = _aggregate(session_rows, (lambda s: _bounded(s, KNOWN_SESSION_STATES),), combine=_sum)
    for (state,), count in sorted(sessions_by_state.items()):
        lines.append(_metric_line("call_audit_live_sessions", {"state": state}, count))

    lines.append(_header("call_audit_event_outbox_events", "gauge", "Total rows currently retained in the durable per-tenant event outbox."))
    lines.append(_metric_line("call_audit_event_outbox_events", {}, max(0, int(outbox_row[0])) if outbox_row else 0))

    lines.append(_header("call_audit_database_up", "gauge", "Whether the most recent metrics scrape's database queries succeeded (1) or failed (0)."))
    lines.append(_metric_line("call_audit_database_up", {}, 1))

    return "\n".join(lines) + "\n"
