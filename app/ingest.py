import hashlib
import json
import os
import subprocess
import tempfile
import wave
from pathlib import Path
from uuid import uuid4

from app.db import connect
from app.auth import Scope
from app.storage import LocalPrivateStorage

MAX_AUDIO_BYTES = 250 * 1024 * 1024
MAX_DURATION_MS = 120 * 60 * 1000


class IntakeError(ValueError):
    pass


class IdempotencyConflict(IntakeError):
    pass


def inspect_audio(audio: bytes) -> tuple[str, int, int, int]:
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise IntakeError("Audio size is invalid")
    if audio.startswith(b"RIFF") and audio[8:12] == b"WAVE":
        try:
            with wave.open(__import__("io").BytesIO(audio), "rb") as source:
                channels, rate, frames = source.getnchannels(), source.getframerate(), source.getnframes()
                duration = round(frames * 1000 / rate) if rate else 0
                if source.getcomptype() != "NONE" or not 0 < channels <= 2 or rate <= 0 or duration <= 0 or duration > MAX_DURATION_MS:
                    raise IntakeError("Unsupported WAV properties")
                return "wav", rate, channels, duration
        except (wave.Error, EOFError) as error:
            raise IntakeError("Invalid WAV data") from error
    if audio.startswith(b"ID3") or (len(audio) > 1 and audio[0] == 0xff and audio[1] & 0xe0 == 0xe0):
        # ffprobe receives a private temporary file, never a shell command or user path.
        with tempfile.NamedTemporaryFile(suffix=".mp3") as source:
            source.write(audio)
            source.flush()
            try:
                result = subprocess.run([os.environ.get("FFPROBE", "ffprobe"), "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=sample_rate,channels:format=duration", "-of", "json", source.name], capture_output=True, timeout=5, check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                metadata = json.loads(result.stdout)
                stream = metadata["streams"][0]
                rate, channels, duration = int(stream["sample_rate"]), int(stream["channels"]), round(float(metadata["format"]["duration"]) * 1000)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError, json.JSONDecodeError) as error:
                raise IntakeError("Invalid MP3 data") from error
            if not 0 < channels <= 2 or rate <= 0 or duration <= 0 or duration > MAX_DURATION_MS:
                raise IntakeError("Unsupported MP3 properties")
            return "mp3", rate, channels, duration
    raise IntakeError("Unsupported audio format")


def accept_recording(scope: Scope, external_ref: str, audio: bytes, metadata: dict, idempotency_key: str, *, storage: LocalPrivateStorage | None = None) -> dict:
    if not idempotency_key or len(idempotency_key) > 200 or not external_ref or len(external_ref) > 300:
        raise IntakeError("Invalid idempotency key or external reference")
    codec, rate, channels, duration = inspect_audio(audio)
    checksum = hashlib.sha256(audio).hexdigest()
    storage = storage or LocalPrivateStorage(os.environ.get("AUDIO_STORAGE_PATH", "./private-audio"))
    key = storage.put(audio)
    call_id, audio_id, job_id = uuid4(), uuid4(), uuid4()
    try:
        with connect() as connection:
            with connection.transaction():
                existing = connection.execute("SELECT id,payload_sha256,processing_state FROM calls WHERE organisation_id=%s AND idempotency_key=%s FOR UPDATE", (scope.organisation_id, idempotency_key)).fetchone()
                if existing:
                    if existing[1] != checksum:
                        raise IdempotencyConflict("Idempotency key reused with different audio")
                    storage.delete(key)
                    return {"id": str(existing[0]), "processing_state": existing[2]}
                connection.execute("INSERT INTO calls(organisation_id,id,external_ref,idempotency_key,payload_sha256,agent_id,team_id,language,processing_state) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'QUEUED')", (scope.organisation_id, call_id, external_ref, idempotency_key, checksum, str(metadata.get("agent_id", "")), str(metadata.get("team_id", "")), str(metadata.get("language", "und"))))
                connection.execute("INSERT INTO audio_objects(organisation_id,id,call_id,private_key,checksum,codec,sample_rate,channels,duration_ms) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)", (scope.organisation_id, audio_id, call_id, key, checksum, codec, rate, channels, duration))
                connection.execute("INSERT INTO jobs(organisation_id,id,call_id,stage,state) VALUES (%s,%s,%s,'TRANSCRIBE','QUEUED')", (scope.organisation_id, job_id, call_id))
    except Exception:
        storage.delete(key)
        raise
    return {"id": str(call_id), "processing_state": "QUEUED"}


def claim_job(connection, worker_id: str, lease_seconds: int = 60) -> dict | None:
    if not worker_id or not 1 <= lease_seconds <= 3600:
        raise ValueError("invalid worker lease")
    with connection.transaction():
        row = connection.execute("WITH selected AS (SELECT organisation_id,id FROM jobs WHERE (state='QUEUED' AND available_at<=now()) OR (state='RUNNING' AND lease_until<now()) ORDER BY available_at,id FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE jobs j SET state='RUNNING',lease_token=%s,lease_until=now()+make_interval(secs=>%s),attempts=attempts+1 FROM selected s WHERE j.organisation_id=s.organisation_id AND j.id=s.id AND EXISTS (SELECT 1 FROM calls c WHERE c.organisation_id=j.organisation_id AND c.id=j.call_id AND c.tombstoned_at IS NULL) RETURNING j.*", (uuid4(), lease_seconds)).fetchone()
    if row is None:
        return None
    columns = [item.name for item in connection.execute("SELECT * FROM jobs LIMIT 0").description]
    return dict(zip(columns, row, strict=True))


def finish_job(connection, job_id: str, lease_token: str, result: dict) -> bool:
    with connection.transaction():
        changed = connection.execute("UPDATE jobs j SET state='DONE',lease_token=NULL,lease_until=NULL WHERE j.id=%s AND j.lease_token=%s AND j.state='RUNNING' AND j.lease_until>now() AND EXISTS (SELECT 1 FROM calls c WHERE c.organisation_id=j.organisation_id AND c.id=j.call_id AND c.tombstoned_at IS NULL)", (job_id, lease_token)).rowcount
    return changed == 1
