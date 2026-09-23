"""Lease-safe queue drain. Model stages register a processor when implemented."""

import os
import logging
import math
import signal
import socket
import threading
import time
from collections.abc import Callable, Mapping

from app.db import connect
from app.ingest import MAX_JOB_ATTEMPTS, claim_job, defer_job, finish_job, renew_job, retry_job
from app.storage import LocalPrivateStorage
from app.disposition_config import compile_disposition_config
from app.disposition import classify_disposition
from app.compliance import evaluate_rules, load_ruleset
from app.audit import audit_call, load_pinned_text, load_rubric

Processor = Callable[[dict], str | dict]
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 20
_LOGGER = logging.getLogger(__name__)
_KNOWN_STAGES = frozenset({"TRANSCRIBE", "ANALYSE", "POLICY", "AUDIT"})
_KNOWN_OUTCOMES = frozenset({"WAITING_HANDLER", "LEASE_LOST", "COMMITTED", "STALE_COMMIT", "RETRY_HANDLED", "RETRY_HANDLER_FAILED"})


def _log_stage_outcome(stage, attempt, outcome, started_at: float) -> None:
    safe_stage = stage if isinstance(stage, str) and stage in _KNOWN_STAGES else "UNKNOWN"
    safe_attempt = attempt if isinstance(attempt, int) and not isinstance(attempt, bool) else 0
    safe_attempt = min(MAX_JOB_ATTEMPTS, max(0, safe_attempt))
    safe_outcome = outcome if outcome in _KNOWN_OUTCOMES else "UNKNOWN"
    duration_ms = min(31_536_000_000, max(0, round((time.monotonic() - started_at) * 1000)))
    _LOGGER.info(
        "worker stage outcome",
        extra={
            "event_name": "worker.stage_outcome",
            "stage": safe_stage,
            "attempt": safe_attempt,
            "outcome": safe_outcome,
            "duration_ms": duration_ms,
        },
    )


def make_disposition_processor(adapter):
    """Build ANALYSE processor over the current redacted final transcript."""
    def process(job: dict) -> dict:
        with connect() as connection:
            active = connection.execute(
                "SELECT v.config_id,v.version,v.raw_json,c.transcript_revision "
                "FROM active_disposition_configs a JOIN disposition_config_versions v "
                "ON v.organisation_id=a.organisation_id AND v.config_id=a.config_id AND v.version=a.version "
                "JOIN calls c ON c.organisation_id=a.organisation_id "
                "WHERE a.organisation_id=%s AND c.id=%s AND c.tombstoned_at IS NULL",
                (job["organisation_id"], job["call_id"]),
            ).fetchone()
            if active is None or active[3] != job["input_revision"]:
                raise RuntimeError("disposition config unavailable or transcript revision stale")
            rows = connection.execute(
                "SELECT id,speaker_id,role,start_ms,end_ms,text_redacted,is_final FROM transcript_utterances "
                "WHERE organisation_id=%s AND call_id=%s AND revision=%s ORDER BY start_ms,id",
                (job["organisation_id"], job["call_id"], job["input_revision"]),
            ).fetchall()
        transcript = [{"id": row[0], "speaker_id": row[1], "role": row[2], "start_ms": row[3], "end_ms": row[4], "text_redacted": row[5], "is_final": row[6]} for row in rows]
        config = compile_disposition_config(active[2])
        decision = classify_disposition(transcript, {}, adapter, config)
        output = decision.as_dict()
        output["transcript_revision"] = job["input_revision"]
        return {"disposition": output}
    return process


def make_policy_processor(ruleset):
    """Build the deterministic POLICY stage over final redacted utterances."""
    def process(job: dict) -> dict:
        with connect() as connection:
            call = connection.execute(
                "SELECT c.transcript_revision,c.call_type,c.agent_connected_ms,c.tagged_intervals,c.call_complete,c.timing_reliable,a.duration_ms "
                "FROM calls c LEFT JOIN audio_objects a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
                "WHERE c.organisation_id=%s AND c.id=%s AND c.tombstoned_at IS NULL",
                (job["organisation_id"], job["call_id"]),
            ).fetchone()
            if call is None or call[0] != job["input_revision"]:
                raise RuntimeError("policy transcript revision is stale or unavailable")
            rows = connection.execute(
                "SELECT id,role,start_ms,end_ms,text_redacted,is_final FROM transcript_utterances "
                "WHERE organisation_id=%s AND call_id=%s AND revision=%s ORDER BY start_ms,id",
                (job["organisation_id"], job["call_id"], job["input_revision"]),
            ).fetchall()
        utterances = [{"id": row[0], "role": row[1], "start_ms": row[2], "end_ms": row[3], "text_redacted": row[4], "is_final": row[5]} for row in rows]
        context = {
            "organisation_id": str(job["organisation_id"]),
            "call_id": str(job["call_id"]),
            "transcript_revision": job["input_revision"],
            "call_type": call[1],
            "agent_connected_ms": call[2],
            "holds": call[3],
            "complete": call[4],
            "timing_reliable": call[5],
            "call_duration_ms": call[6] or 0,
        }
        return {"findings": evaluate_rules(utterances, context, ruleset)}
    return process


def make_audit_processor(adapter, rubric, prompt: str, *, prompt_version: str = "audit_prompt_v1"):
    """Build AUDIT over one tenant-scoped final transcript revision."""
    def process(job: dict) -> dict:
        with connect() as connection:
            call_row = connection.execute(
                "SELECT c.transcript_revision,c.call_type,c.language FROM calls c "
                "WHERE c.organisation_id=%s AND c.id=%s AND c.tombstoned_at IS NULL",
                (job["organisation_id"], job["call_id"]),
            ).fetchone()
            if call_row is None or call_row[0] != job["input_revision"]:
                raise RuntimeError("audit transcript revision is stale or unavailable")
            rows = connection.execute(
                "SELECT id,role,start_ms,end_ms,text_redacted,is_final FROM transcript_utterances "
                "WHERE organisation_id=%s AND call_id=%s AND revision=%s ORDER BY start_ms,id",
                (job["organisation_id"], job["call_id"], job["input_revision"]),
            ).fetchall()
            finding_rows = connection.execute(
                "SELECT rule_id,ruleset_version,ruleset_hash,status,severity,evidence_ids FROM findings WHERE organisation_id=%s AND call_id=%s AND transcript_revision=%s ORDER BY rule_id",
                (job["organisation_id"], job["call_id"], job["input_revision"]),
            ).fetchall()
        utterances = [{"id": row[0], "role": row[1], "start_ms": row[2], "end_ms": row[3], "text_redacted": row[4], "is_final": row[5]} for row in rows]
        findings = [{"rule_id": row[0], "ruleset_version": row[1], "ruleset_hash": row[2], "status": row[3], "severity": row[4], "evidence_ids": row[5]} for row in finding_rows]
        call = {"organisation_id": str(job["organisation_id"]), "call_id": str(job["call_id"]), "transcript_revision": job["input_revision"], "call_type": call_row[1], "language": call_row[2]}
        return {"audit": audit_call(call, utterances, findings, adapter, rubric, prompt, prompt_version=prompt_version)}
    return process


def run_once(worker_id: str, processors: Mapping[str, Processor] | None = None) -> bool:
    processors = processors or {}
    with connect() as connection:
        job = claim_job(connection, worker_id, LEASE_SECONDS, tuple(processors))
        if job is None:
            return False
        processor = processors.get(job["stage"])
        job_id, token = str(job["id"]), str(job["lease_token"])
        if processor is None:
            started_at = time.monotonic()
            deferred = defer_job(connection, job_id, token)
            _log_stage_outcome(job.get("stage"), job.get("attempts"), "WAITING_HANDLER" if deferred else "LEASE_LOST", started_at)
            return True

        started_at = time.monotonic()
        outcome = "LEASE_LOST"
        stopping, lease_lost = threading.Event(), threading.Event()

        def heartbeat() -> None:
            while not stopping.wait(HEARTBEAT_SECONDS):
                try:
                    with connect() as heartbeat_connection:
                        if not renew_job(heartbeat_connection, job_id, token, LEASE_SECONDS):
                            lease_lost.set()
                            return
                except Exception:
                    lease_lost.set()
                    return

        thread = threading.Thread(target=heartbeat, name=f"lease-{job_id}", daemon=True)
        thread.start()
        try:
            output = processor(job)
            result = output if isinstance(output, dict) else {"processing_state": output}
            if lease_lost.is_set():
                outcome = "LEASE_LOST"
            else:
                outcome = "COMMITTED" if finish_job(connection, job_id, token, result) else "STALE_COMMIT"
        except Exception:
            try:
                outcome = "RETRY_HANDLED" if retry_job(connection, job_id, token) else "LEASE_LOST"
            except Exception:
                outcome = "RETRY_HANDLER_FAILED"
                raise
        finally:
            stopping.set()
            thread.join(timeout=HEARTBEAT_SECONDS + 1)
            _log_stage_outcome(job.get("stage"), job.get("attempts"), outcome, started_at)
    return True


def drain(worker_id: str | None = None, processors: Mapping[str, Processor] | None = None) -> int:
    worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}"
    with connect() as connection:
        referenced = {row[0] for row in connection.execute("SELECT private_key FROM audio_objects")}
    # ponytail: full key scan at worker startup; bound it only if object counts warrant it.
    LocalPrivateStorage(os.environ.get("AUDIO_STORAGE_PATH", "./private-audio")).delete_orphans(referenced)
    completed = 0
    while run_once(worker_id, processors):
        completed += 1
    return completed


def build_processors() -> dict[str, Processor]:
    """Build only locally configured handlers; absent model handlers stay parked."""
    from app.transcription import make_transcription_processor

    processors = {}
    policy_ruleset = None
    policy_path = os.environ.get("POLICY_RULESET_PATH")
    policy_digest = os.environ.get("POLICY_RULESET_SHA256")
    if policy_path or policy_digest:
        if not policy_path or not policy_digest:
            raise RuntimeError("Both POLICY_RULESET_PATH and POLICY_RULESET_SHA256 are required")
        policy_ruleset = load_ruleset(policy_path, policy_digest)
        processors["POLICY"] = make_policy_processor(policy_ruleset)
    audit_vars = ("AUDIT_MODEL_PATH", "AUDIT_MODEL_SHA256", "AUDIT_LOCAL_URL", "AUDIT_LOCAL_MODEL_NAME", "AUDIT_RUBRIC_PATH", "AUDIT_RUBRIC_SHA256", "AUDIT_PROMPT_PATH", "AUDIT_PROMPT_SHA256")
    configured_audit = any(os.environ.get(name) for name in audit_vars)
    if configured_audit:
        if not all(os.environ.get(name) for name in audit_vars):
            raise RuntimeError("All AUDIT model, rubric and prompt pin settings are required")
        from app.audit import LocalVllmAuditAdapter

        audit_rubric = load_rubric(os.environ["AUDIT_RUBRIC_PATH"], os.environ["AUDIT_RUBRIC_SHA256"])
        audit_prompt = load_pinned_text(os.environ["AUDIT_PROMPT_PATH"], os.environ["AUDIT_PROMPT_SHA256"])
        processors["AUDIT"] = make_audit_processor(LocalVllmAuditAdapter.from_environment(), audit_rubric, audit_prompt, prompt_version=os.environ.get("AUDIT_PROMPT_VERSION", "audit_prompt_v1"))
    transcription_vars = ("FASTER_WHISPER_MODEL_PATH", "FASTER_WHISPER_MODEL_SHA256", "FASTER_WHISPER_MODEL_VERSION")
    configured_transcription = any(os.environ.get(name) for name in transcription_vars)
    if configured_transcription and not all(os.environ.get(name) for name in transcription_vars):
        raise RuntimeError("All FASTER_WHISPER model path, checksum and version settings are required")
    if configured_transcription:
        processors["TRANSCRIBE"] = make_transcription_processor(ruleset=policy_ruleset)
    if os.environ.get("DISPOSITION_MODEL_SHA256"):
        from app.local_disposition_adapter import LocalVllmDispositionAdapter

        # Configuration is deployment-only and requires an immutable artifact digest.
        # Without it, ANALYSE remains parked as WAITING_HANDLER rather than using a fake.
        processors["ANALYSE"] = make_disposition_processor(LocalVllmDispositionAdapter.from_environment())
    return processors


def run_forever(
    worker_id: str,
    processors: Mapping[str, Processor],
    *,
    idle_poll_seconds: float = 1.0,
    stop_event: threading.Event | None = None,
) -> None:
    """Poll continuously, sleeping when idle, with a bounded configured interval."""
    if isinstance(idle_poll_seconds, bool) or not math.isfinite(idle_poll_seconds) or not 0.5 <= idle_poll_seconds <= 10:
        raise ValueError("idle poll interval must be between 0.5 and 10 seconds")
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        if not run_once(worker_id, processors):
            stop_event.wait(idle_poll_seconds)


def main() -> None:
    worker_id = f"{socket.gethostname()}-{os.getpid()}"
    processors = build_processors()
    with connect() as connection:
        referenced = {row[0] for row in connection.execute("SELECT private_key FROM audio_objects")}
    # Keep startup orphan cleanup once per process, not on every idle poll.
    LocalPrivateStorage(os.environ.get("AUDIO_STORAGE_PATH", "./private-audio")).delete_orphans(referenced)

    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())
    try:
        idle_poll = float(os.environ.get("WORKER_IDLE_POLL_SECONDS", "1"))
    except ValueError as error:
        raise RuntimeError("WORKER_IDLE_POLL_SECONDS must be a number") from error
    run_forever(worker_id, processors, idle_poll_seconds=idle_poll, stop_event=stop_event)


if __name__ == "__main__":
    main()
