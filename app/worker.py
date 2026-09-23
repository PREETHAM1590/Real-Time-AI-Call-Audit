"""Lease-safe queue drain. Model stages register a processor when implemented."""

import os
import socket
import threading
from collections.abc import Callable, Mapping

from app.db import connect
from app.ingest import claim_job, defer_job, finish_job, renew_job, retry_job
from app.storage import LocalPrivateStorage
from app.disposition_config import compile_disposition_config
from app.disposition import classify_disposition
from app.compliance import evaluate_rules, load_ruleset

Processor = Callable[[dict], str | dict]
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 20


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


def run_once(worker_id: str, processors: Mapping[str, Processor] | None = None) -> bool:
    processors = processors or {}
    with connect() as connection:
        job = claim_job(connection, worker_id, LEASE_SECONDS, tuple(processors))
        if job is None:
            return False
        processor = processors.get(job["stage"])
        job_id, token = str(job["id"]), str(job["lease_token"])
        if processor is None:
            defer_job(connection, job_id, token)
            return True

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
            if not lease_lost.is_set():
                finish_job(connection, job_id, token, result)
        except Exception:
            retry_job(connection, job_id, token)
        finally:
            stopping.set()
            thread.join(timeout=HEARTBEAT_SECONDS + 1)
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


if __name__ == "__main__":
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
    if os.environ.get("FASTER_WHISPER_MODEL_PATH") and os.environ.get("FASTER_WHISPER_MODEL_SHA256") and os.environ.get("FASTER_WHISPER_MODEL_VERSION"):
        processors["TRANSCRIBE"] = make_transcription_processor(ruleset=policy_ruleset)
    if os.environ.get("DISPOSITION_MODEL_SHA256"):
        from app.local_disposition_adapter import LocalVllmDispositionAdapter

        # Configuration is deployment-only and requires an immutable artifact digest.
        # Without it, ANALYSE remains parked as WAITING_HANDLER rather than using a fake.
        processors["ANALYSE"] = make_disposition_processor(LocalVllmDispositionAdapter.from_environment())
    print(f"Drained {drain(processors=processors)} job(s); local model artifacts must be provisioned and pinned before transcription.")
