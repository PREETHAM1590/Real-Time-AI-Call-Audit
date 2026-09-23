from collections.abc import Mapping

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware

from app.auth import IdentityLookup, Scope, can_access, scope_dependency
from app.config import Settings
from app.ingest import IdempotencyConflict, IntakeError, MAX_AUDIO_BYTES, accept_recording

CallScope = tuple[str, str, str]


def create_app(
    settings: Settings,
    *,
    identity_lookup: IdentityLookup | None = None,
    call_scopes: Mapping[str, CallScope] | None = None,
) -> FastAPI:
    """Create the API with OIDC validation and server-owned scope lookups."""
    app = FastAPI()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    resolve_identity = identity_lookup or (lambda _subject: None)
    records = call_scopes or {}
    get_scope = scope_dependency(settings, resolve_identity)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/calls/{call_id}")
    async def get_call(call_id: str, scope: Scope = Depends(get_scope)) -> dict[str, str]:
        record = records.get(call_id)
        if record is None or not can_access(scope, *record):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        return {"id": call_id}

    @app.post("/v1/calls", status_code=status.HTTP_202_ACCEPTED)
    async def upload_call(
        audio: UploadFile = File(...),
        external_ref: str = Form(...),
        agent_id: str = Form(...),
        team_id: str = Form(...),
        language: str = Form("und"),
        idempotency_key: str = Header(alias="Idempotency-Key"),
        scope: Scope = Depends(get_scope),
    ) -> dict[str, str]:
        payload = bytearray()
        while block := await audio.read(1024 * 1024):
            if len(payload) + len(block) > MAX_AUDIO_BYTES:
                raise HTTPException(status_code=413, detail="Audio is too large")
            payload.extend(block)
        try:
            result = accept_recording(scope, external_ref, bytes(payload), {"agent_id": agent_id, "team_id": team_id, "language": language}, idempotency_key)
        except IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail="Idempotency key conflicts with existing audio") from error
        except IntakeError as error:
            raise HTTPException(status_code=400, detail="Invalid audio") from error
        return result

    return app
