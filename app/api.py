import asyncio
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from app.auth import IdentityLookup, Scope, can_access, scope_dependency
from app.config import Settings
from app.db import connect
from app.ingest import IdempotencyConflict, IntakeError, MAX_AUDIO_BYTES, accept_recording


def create_app(
    settings: Settings,
    *,
    identity_lookup: IdentityLookup | None = None,
) -> FastAPI:
    """Create the API with OIDC validation and server-owned scope lookups."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
    )
    resolve_identity = identity_lookup or (lambda _subject: None)
    get_scope = scope_dependency(settings, resolve_identity)
    intake_slots = asyncio.Semaphore(4)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/calls/{call_id}")
    async def get_call(call_id: str, scope: Scope = Depends(get_scope)) -> dict[str, str]:
        with connect() as connection:
            record = connection.execute("SELECT id,organisation_id,agent_id,team_id,processing_state FROM calls WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL", (scope.organisation_id, call_id)).fetchone()
        if record is None or not can_access(scope, str(record[1]), record[2], record[3]):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return {"id": str(record[0]), "processing_state": record[4]}

    @app.post("/v1/calls", status_code=status.HTTP_202_ACCEPTED)
    async def upload_call(
        audio: UploadFile = File(...),
        external_ref: str = Form(...),
        language: str = Form("und"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
        scope: Scope = Depends(get_scope),
    ) -> dict[str, str]:
        if scope.role != "AGENT" or len(scope.team_ids) != 1:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Upload requires an agent identity with one server-resolved team")
        async with intake_slots:
            payload = bytearray()
            while block := await audio.read(1024 * 1024):
                if len(payload) + len(block) > MAX_AUDIO_BYTES:
                    raise HTTPException(status_code=413, detail="Audio is too large")
                payload.extend(block)
            try:
                result = await run_in_threadpool(accept_recording, scope, external_ref, bytes(payload), {"language": language}, idempotency_key)
            except IdempotencyConflict as error:
                raise HTTPException(status_code=409, detail="Idempotency key conflicts with existing audio") from error
            except IntakeError as error:
                raise HTTPException(status_code=400, detail="Invalid audio") from error
        return result

    return app
