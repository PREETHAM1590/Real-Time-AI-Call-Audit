"""Lease-safe queue drain. Model stages register a processor when implemented."""

import os
import socket
import threading
from collections.abc import Callable, Mapping

from app.db import connect
from app.ingest import claim_job, defer_job, finish_job, renew_job, retry_job
from app.storage import LocalPrivateStorage

Processor = Callable[[dict], str]
LEASE_SECONDS = 60
HEARTBEAT_SECONDS = 20


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
            state = processor(job)
        except Exception:
            retry_job(connection, job_id, token)
        else:
            if not lease_lost.is_set():
                finish_job(connection, job_id, token, {"processing_state": state})
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
    print(f"Drained {drain()} job(s); TRANSCRIBE awaits a configured local processor.")
