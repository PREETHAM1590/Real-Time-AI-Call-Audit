import threading

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool

from app.auth import IdentityLookup, Scope, can_access, csrf_token_for_session, scope_dependency
from app.config import Settings
from app.db import connect
from app.ingest import IdempotencyConflict, IntakeError, MAX_AUDIO_BYTES, accept_recording

MULTIPART_OVERHEAD_BYTES = 64 * 1024


class _RequestBodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Bound upload bytes and parsing concurrency before multipart spooling begins."""

    def __init__(self, app, *, max_bytes: int, get_scope, concurrent_requests: int = 4):
        self.app = app
        self.max_bytes = max_bytes
        self.get_scope = get_scope
        self.slots = threading.BoundedSemaphore(concurrent_requests)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != "/v1/calls":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", ()))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > self.max_bytes:
            await JSONResponse({"detail": "Audio is too large"}, status_code=413)(scope, receive, send)
            return
        if not self.slots.acquire(blocking=False):
            response = JSONResponse({"detail": "Upload capacity is busy"}, status_code=503, headers={"Retry-After": "1"})
            await response(scope, receive, send)
            return
        try:
            try:
                auth_scope = await self.get_scope(Request(scope, receive))
            except HTTPException as error:
                await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
                return
            if auth_scope.role != "AGENT" or len(auth_scope.team_ids) != 1:
                await JSONResponse({"detail": "Upload requires an agent identity with one server-resolved team"}, status_code=403)(scope, receive, send)
                return
            scope.setdefault("state", {})["auth_scope"] = auth_scope
            received = 0

            async def limited_receive():
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_bytes:
                        raise _RequestBodyTooLarge
                return message

            try:
                await self.app(scope, limited_receive, send)
            except _RequestBodyTooLarge:
                await JSONResponse({"detail": "Audio is too large"}, status_code=413)(scope, receive, send)
        finally:
            self.slots.release()


def create_app(
    settings: Settings,
    *,
    identity_lookup: IdentityLookup | None = None,
) -> FastAPI:
    """Create the API with OIDC validation and server-owned scope lookups."""
    app = FastAPI()
    get_scope = scope_dependency(settings, identity_lookup)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/csrf")
    async def get_csrf_token(request: Request, response: Response, _scope: Scope = Depends(get_scope)) -> dict[str, str]:
        session_token = request.cookies.get("session")
        if not session_token or request.headers.get("Authorization"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cookie session required")
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": csrf_token_for_session(session_token, settings.csrf_secret)}

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

    limited_app = UploadBodyLimitMiddleware(app, max_bytes=MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES, get_scope=get_scope)
    return CORSMiddleware(
        limited_app,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Organisation-ID"],
    )
