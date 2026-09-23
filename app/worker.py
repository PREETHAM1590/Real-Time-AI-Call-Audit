"""Lease-safe queue drain. Model stages register a processor when implemented."""

import os
import socket
from collections.abc import Callable, Mapping

from app.db import connect
from app.ingest import claim_job, finish_job
from app.storage import LocalPrivateStorage

Processor = Callable[[dict], str]


def run_once(worker_id: str, processors: Mapping[str, Processor] | None = None) -> bool:
    processors = processors or {}
    with connect() as connection:
        job = claim_job(connection, worker_id)
        if job is None:
            return False
        processor = processors.get(job["stage"])
        # Until Task 3 registers local transcription, expose an explicit review state;
        # never mark an unprocessed recording READY or leave its lease running forever.
        state = processor(job) if processor else "NEEDS_REVIEW"
        finish_job(connection, str(job["id"]), str(job["lease_token"]), {"processing_state": state})
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
    print(f"Drained {drain()} job(s); TRANSCRIBE awaits a configured local processor.")
