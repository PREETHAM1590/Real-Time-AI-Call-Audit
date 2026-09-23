import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import wave
from pathlib import Path
from uuid import uuid4

from app.db import connect
from app.auth import Scope
from app.contracts import PersistedUtterance
from app.storage import LocalPrivateStorage

MAX_AUDIO_BYTES = 250 * 1024 * 1024
MAX_DURATION_MS = 120 * 60 * 1000
MAX_JOB_ATTEMPTS = 5
_MP3_DECODERS = threading.BoundedSemaphore(2)


class IntakeError(ValueError):
    pass


class IdempotencyConflict(IntakeError):
    pass


def inspect_audio(audio: bytes) -> tuple[str, int, int, int]:
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise IntakeError("Audio size is invalid")
    if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
        try:
            with wave.open(io.BytesIO(audio), "rb") as source:
                channels, rate, frames = source.getnchannels(), source.getframerate(), source.getnframes()
                duration = round(frames * 1000 / rate) if rate else 0
                if source.getcomptype() != "NONE" or not 0 < channels <= 2 or rate <= 0 or duration <= 0 or duration > MAX_DURATION_MS:
                    raise IntakeError("Unsupported WAV properties")
                if len(source.readframes(frames)) != frames * channels * source.getsampwidth():
                    raise IntakeError("Truncated WAV data")
                return "wav", rate, channels, duration
        except (wave.Error, EOFError) as error:
            raise IntakeError("Invalid WAV data") from error
    if audio.startswith(b"ID3") or (len(audio) > 1 and audio[0] == 0xff and audio[1] & 0xe0 == 0xe0):
        # Close before child processes for Windows; never pass upload-derived paths or shell text.
        if not _MP3_DECODERS.acquire(timeout=5):
            raise IntakeError("Audio validation capacity is busy")
        source_name = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as source:
                source_name = source.name
                source.write(audio)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run([os.environ.get("FFPROBE", "ffprobe"), "-v", "error", "-select_streams", "a:0", "-count_frames", "-show_entries", "stream=sample_rate,channels,nb_read_frames:format=duration", "-of", "json", source_name], capture_output=True, timeout=30, check=True, creationflags=flags)
            metadata = json.loads(result.stdout)
            stream = metadata["streams"][0]
            rate, channels, duration = int(stream["sample_rate"]), int(stream["channels"]), round(float(metadata["format"]["duration"]) * 1000)
            if not 0 < channels <= 2 or rate <= 0 or duration <= 0 or duration > MAX_DURATION_MS:
                raise IntakeError("Unsupported MP3 properties")
            frame_samples = 1152 if rate > 24000 else 576
            expected_frames = (duration * rate + frame_samples * 1000 - 1) // (frame_samples * 1000)
            if int(stream["nb_read_frames"]) + 2 < expected_frames:
                raise IntakeError("Truncated MP3 data")
            decode_timeout = min(MAX_DURATION_MS // 1000, max(30, duration // 1000 * 3 + 10))
            subprocess.run([os.environ.get("FFMPEG", "ffmpeg"), "-nostdin", "-v", "error", "-xerror", "-i", source_name, "-f", "null", "-"], capture_output=True, timeout=decode_timeout, check=True, creationflags=flags)
            return "mp3", rate, channels, duration
        except IntakeError:
            raise
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
            raise IntakeError("Invalid MP3 data") from error
        finally:
            try:
                if source_name is not None:
                    try:
                        os.unlink(source_name)
                    except FileNotFoundError:
                        pass
            finally:
                _MP3_DECODERS.release()
    raise IntakeError("Unsupported audio format")


def accept_recording(scope: Scope, external_ref: str, audio: bytes, metadata: dict, idempotency_key: str, *, storage: LocalPrivateStorage | None = None) -> dict:
    if scope.role != "AGENT" or not scope.organisation_id.strip() or not scope.user_id.strip() or len(scope.team_ids) != 1 or not next(iter(scope.team_ids)).strip():
        raise IntakeError("Recording intake requires an agent identity and one server-resolved team")
    if not idempotency_key or len(idempotency_key) > 200 or not external_ref or len(external_ref) > 300:
        raise IntakeError("Invalid idempotency key or external reference")
    codec, rate, channels, duration = inspect_audio(audio)
    checksum = hashlib.sha256(audio).hexdigest()
    storage = storage or LocalPrivateStorage(os.environ.get("AUDIO_STORAGE_PATH", "./private-audio"))
    key = storage.put(audio)
    call_id, audio_id, job_id = uuid4(), uuid4(), uuid4()
    result = None
    try:
        with connect() as connection:
            with connection.transaction():
                inserted = connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED') ON CONFLICT (organisation_id,idempotency_key) DO NOTHING RETURNING id", (scope.organisation_id, call_id, external_ref, idempotency_key, checksum, scope.user_id if scope.role == "AGENT" else "", next(iter(scope.team_ids)) if scope.role == "AGENT" and len(scope.team_ids) == 1 else "", str(metadata.get("language", "und")))).fetchone()
                if inserted is None:
                    existing = connection.execute("SELECT id,payload_sha256,processing_state FROM calls WHERE organisation_id=%s AND idempotency_key=%s", (scope.organisation_id, idempotency_key)).fetchone()
                    if existing is None:
                        raise IntakeError("Call reference already exists")
                    if existing[1] != checksum:
                        raise IdempotencyConflict("Idempotency key reused with different audio")
                    result = {"id": str(existing[0]), "processing_state": existing[2]}
                else:
                    connection.execute("INSERT INTO audio_objects(organisation_id,id,call_id,private_key,checksum,codec,sample_rate,channels,duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (scope.organisation_id, audio_id, call_id, key, checksum, codec, rate, channels, duration))
                    connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state) VALUES (%s,%s,%s,'TRANSCRIBE','QUEUED')", (scope.organisation_id, job_id, call_id))
    except Exception:
        storage.delete(key)
        raise
    if result is not None:
        storage.delete(key)
        return result
    return {"id": str(call_id), "processing_state": "QUEUED"}


def claim_job(connection, worker_id: str, lease_seconds: int = 60, supported_stages=None) -> dict | None:
    if not worker_id or not 1 <= lease_seconds <= 3600:
        raise ValueError("invalid worker lease")
    if supported_stages is not None and not supported_stages:
        return None
    stage_filter = "AND j.stage=ANY(%s)" if supported_stages is not None else ""
    params = (MAX_JOB_ATTEMPTS, list(supported_stages), uuid4(), lease_seconds) if supported_stages is not None else (MAX_JOB_ATTEMPTS, uuid4(), lease_seconds)
    with connection.transaction():
        connection.execute("UPDATE jobs SET state='FAILED',lease_token=NULL,lease_until=NULL,last_error_code='RETRIES_EXHAUSTED' WHERE state='RUNNING' AND attempts >= %s AND lease_until < now()", (MAX_JOB_ATTEMPTS,))
        connection.execute("UPDATE calls c SET processing_state='FAILED' FROM jobs j WHERE j.organisation_id=c.organisation_id AND j.call_id=c.id AND j.state='FAILED' AND j.last_error_code='RETRIES_EXHAUSTED' AND c.tombstoned_at IS NULL")
        query = f"WITH selected AS (SELECT j.organisation_id,j.id FROM jobs j JOIN calls c ON c.organisation_id=j.organisation_id AND c.id=j.call_id WHERE c.tombstoned_at IS NULL AND j.attempts < %s {stage_filter} AND ((j.state IN ('QUEUED','WAITING_HANDLER','RETRY_WAIT') AND j.available_at<=now()) OR (j.state='RUNNING' AND j.lease_until<now())) ORDER BY j.available_at,j.id LIMIT 1 FOR UPDATE OF j,c SKIP LOCKED) UPDATE jobs j SET state='RUNNING',lease_token=%s,lease_until=now()+make_interval(secs=>%s),attempts=attempts+1 FROM selected s WHERE j.organisation_id=s.organisation_id AND j.id=s.id RETURNING j.*"
        row = connection.execute(query, params).fetchone()
    if row is None:
        return None
    columns = [item.name for item in connection.execute("SELECT * FROM jobs LIMIT 0").description]
    return dict(zip(columns, row, strict=True))


def finish_job(connection, job_id: str, lease_token: str, result: dict) -> bool:
    with connection.transaction():
        locked = connection.execute("SELECT c.organisation_id,c.id,c.tombstoned_at,c.transcript_revision FROM calls c JOIN jobs j ON j.organisation_id=c.organisation_id AND j.call_id=c.id WHERE j.id=%s FOR UPDATE OF c", (job_id,)).fetchone()
        if locked is None or locked[2] is not None:
            return False
        changed = connection.execute("UPDATE jobs SET state='DONE',lease_token=NULL,lease_until=NULL WHERE organisation_id=%s AND id=%s AND lease_token=%s AND state='RUNNING' AND lease_until>now()", (locked[0], job_id, lease_token)).rowcount
        if changed == 1:
            state = result.get("processing_state", "NEEDS_REVIEW")
            if "utterances" in result:
                utterances = result["utterances"]
                model_version = result.get("model_version")
                if not isinstance(utterances, list) or not isinstance(model_version, str) or not model_version.strip():
                    raise ValueError("invalid redacted transcript result")
                revision = locked[3] + 1
                for source in utterances:
                    item = PersistedUtterance.model_validate({
                        **source,
                        "organisation_id": str(locked[0]),
                        "call_id": str(locked[1]),
                        "revision": revision,
                        "model_version": model_version,
                    })
                    if item.is_final is not True:
                        raise ValueError("only final transcript utterances can be persisted")
                    connection.execute(
                        "INSERT INTO transcript_utterances(organisation_id,call_id,revision,id,segment_id,speaker_id,role,start_ms,end_ms,text_redacted,confidence,model_version,is_final) "
                        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,true)",
                        (locked[0], locked[1], revision, item.id, item.segment_id, item.speaker_id, item.role, item.start_ms, item.end_ms, item.text_redacted, item.confidence, item.model_version),
                    )
                if state == "ANALYSING" and utterances:
                    connection.execute(
                        "INSERT INTO jobs(organisation_id,id,call_id,stage,input_revision,state) VALUES (%s,%s,%s,'ANALYSE',%s,'QUEUED') ON CONFLICT (organisation_id,call_id,stage,input_revision) DO NOTHING",
                        (locked[0], uuid4(), locked[1], revision),
                    )
                connection.execute("UPDATE calls SET processing_state=%s,transcript_revision=%s WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL", (state, revision, locked[0], locked[1]))
            else:
                connection.execute("UPDATE calls SET processing_state=%s WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL", (state, locked[0], locked[1]))
    return changed == 1


def defer_job(connection, job_id: str, lease_token: str) -> bool:
    """Park work until its processor is installed without consuming a retry attempt."""
    with connection.transaction():
        changed = connection.execute("UPDATE jobs SET state='WAITING_HANDLER',attempts=GREATEST(0,attempts-1),lease_token=NULL,lease_until=NULL WHERE id=%s AND lease_token=%s AND state='RUNNING' AND lease_until>now()", (job_id, lease_token)).rowcount
    return changed == 1


def retry_job(connection, job_id: str, lease_token: str, *, max_attempts: int = MAX_JOB_ATTEMPTS) -> bool:
    """Retry failures with capped exponential delay, then fail the call safely."""
    with connection.transaction():
        locked = connection.execute("SELECT organisation_id,call_id,attempts FROM jobs WHERE id=%s AND lease_token=%s AND state='RUNNING' AND lease_until>now() FOR UPDATE", (job_id, lease_token)).fetchone()
        if locked is None:
            return False
        if locked[2] >= max_attempts:
            connection.execute("UPDATE jobs SET state='FAILED',lease_token=NULL,lease_until=NULL,last_error_code='PROCESSOR_FAILED' WHERE organisation_id=%s AND id=%s", (locked[0], job_id))
            connection.execute("UPDATE calls SET processing_state='FAILED' WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL", (locked[0], locked[1]))
        else:
            delay = min(300, 2 ** max(0, locked[2] - 1))
            connection.execute("UPDATE jobs SET state='RETRY_WAIT',available_at=now()+make_interval(secs=>%s),lease_token=NULL,lease_until=NULL,last_error_code='PROCESSOR_FAILED' WHERE organisation_id=%s AND id=%s", (delay, locked[0], job_id))
            connection.execute("UPDATE calls SET processing_state='RETRY_WAIT' WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL", (locked[0], locked[1]))
    return True


def renew_job(connection, job_id: str, lease_token: str, lease_seconds: int = 60) -> bool:
    if not 1 <= lease_seconds <= 3600:
        raise ValueError("invalid worker lease")
    with connection.transaction():
        changed = connection.execute("UPDATE jobs SET lease_until=now()+make_interval(secs=>%s) WHERE id=%s AND lease_token=%s AND state='RUNNING' AND lease_until>now()", (lease_seconds, job_id, lease_token)).rowcount
    return changed == 1
