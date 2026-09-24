import json
import os
import re
import threading
import asyncio
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
from fastapi import WebSocket, WebSocketDisconnect
from fastapi import Body
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.requests import Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from app.auth import IdentityLookup, Scope, can_access, csrf_token_for_session, scope_dependency
from app.config import Settings
from app.db import connect
from app.ingest import ExternalReferenceConflict, IdempotencyConflict, IntakeError, MAX_AUDIO_BYTES, accept_recording
from app.disposition_config import ConfigError, compile_disposition_config
from app.disposition import classify_disposition
from app.local_disposition_adapter import LocalVllmDispositionAdapter
from app.reviews import ReviewConflict, ReviewForbidden, ReviewNotFound, append_review, call_detail, review_queue
from app.audio_access import issue_audio_capability, verify_audio_capability
from app.storage import LocalPrivateStorage
from app.privacy import redact_text
from app.reports import ReportForbidden, ReportLimitError, ReportPrivacyUnavailable, export_findings, own_scores, team_report
from app.retention import request_call_deletion
from app.operations import operations_summary
from app.events import EventCursorError, EventCursorExpired, EventForbidden, encode_sse, parse_last_event_id, read_events, reset_required_event
from app.exotel import ExotelLifecycleEvent, ExotelProtocolError, ExotelSession
from app.exotel_adapter import build_wav, integration_credentials, make_audio_references, parse_basic_authorization, verify_integration_secret
from app.live_calls import LiveCallsForbidden, activate_exotel_session, read_live_calls, update_exotel_session
from psycopg.errors import UniqueViolation

MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_DISPOSITION_CONFIG_BYTES = 1024 * 1024
MAX_REVIEW_BODY_BYTES = 32 * 1024
# ponytail: this is a per-worker connection cap; use shared admission control if a global cap becomes necessary.
_EXOTEL_CONNECTIONS = threading.BoundedSemaphore(8)
EXOTEL_HANDSHAKE_TIMEOUT_SECONDS = 10.0
EXOTEL_MEDIA_IDLE_TIMEOUT_SECONDS = 30.0
EXOTEL_SESSION_TIMEOUT_SECONDS = 7_230.0


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


class DispositionBodyLimitMiddleware:
    """Authenticate admin config writes before bounded JSON body parsing."""

    def __init__(self, app, *, get_scope, max_bytes: int = MAX_DISPOSITION_CONFIG_BYTES, concurrent_requests: int = 8):
        self.app = app
        self.get_scope = get_scope
        self.max_bytes = max_bytes
        self.slots = threading.BoundedSemaphore(concurrent_requests)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or scope.get("method") != "POST" or not path.startswith("/v1/disposition-configs"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
            return
        if length > self.max_bytes:
            await JSONResponse({"detail": "Configuration request is too large"}, status_code=413)(scope, receive, send)
            return
        if not self.slots.acquire(blocking=False):
            await JSONResponse({"detail": "Configuration API capacity is busy"}, status_code=503, headers={"Retry-After": "1"})(scope, receive, send)
            return
        try:
            try:
                auth_scope = await self.get_scope(Request(scope, receive))
            except HTTPException as error:
                await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
                return
            if auth_scope.role != "ADMIN":
                await JSONResponse({"detail": "Administrator role required"}, status_code=403)(scope, receive, send)
                return
            scope.setdefault("state", {})["auth_scope"] = auth_scope
            count = 0

            async def limited_receive():
                nonlocal count
                message = await receive()
                if message["type"] == "http.request":
                    count += len(message.get("body", b""))
                    if count > self.max_bytes:
                        raise _RequestBodyTooLarge
                return message

            try:
                await self.app(scope, limited_receive, send)
            except _RequestBodyTooLarge:
                await JSONResponse({"detail": "Configuration request is too large"}, status_code=413)(scope, receive, send)
        finally:
            self.slots.release()


class ReviewBodyLimitMiddleware:
    """Authenticate and bound analyst JSON before FastAPI parses it."""

    def __init__(self, app, *, get_scope, max_bytes: int = MAX_REVIEW_BODY_BYTES, concurrent_requests: int = 8):
        self.app = app
        self.get_scope = get_scope
        self.max_bytes = max_bytes
        self.slots = threading.BoundedSemaphore(concurrent_requests)

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope.get("type") != "http" or scope.get("method") != "POST" or not path.startswith("/v1/audits/") or not path.endswith("/reviews"):
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", ()))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(scope, receive, send)
            return
        if content_length < 0 or content_length > self.max_bytes:
            await JSONResponse({"detail": "Review request is too large"}, status_code=413)(scope, receive, send)
            return
        if not self.slots.acquire(blocking=False):
            await JSONResponse({"detail": "Review API capacity is busy"}, status_code=503, headers={"Retry-After": "1"})(scope, receive, send)
            return
        try:
            try:
                auth_scope = await self.get_scope(Request(scope, receive))
            except HTTPException as error:
                await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
                return
            if auth_scope.role not in {"QA_ANALYST", "ADMIN"}:
                await JSONResponse({"detail": "QA analyst permission required"}, status_code=403)(scope, receive, send)
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
                await JSONResponse({"detail": "Review request is too large"}, status_code=413)(scope, receive, send)
        finally:
            self.slots.release()


def create_app(
    settings: Settings,
    *,
    identity_lookup: IdentityLookup | None = None,
    disposition_adapter=None,
    report_redactor=None,
) -> FastAPI:
    """Create the API with OIDC validation and server-owned scope lookups."""
    app = FastAPI()
    get_scope = scope_dependency(settings, identity_lookup)
    app.state.refresh_scope = scope_dependency(settings, identity_lookup, cache_scope=False)
    report_redactor = report_redactor or redact_text
    if disposition_adapter is None and os.environ.get("DISPOSITION_MODEL_SHA256"):
        disposition_adapter = LocalVllmDispositionAdapter.from_environment()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> JSONResponse:
        def database_probe() -> bool:
            try:
                with connect() as connection:
                    return connection.execute("SELECT 1").fetchone() == (1,)
            except Exception:
                return False

        database_available = await run_in_threadpool(database_probe)
        payload = {
            "status": "database_available" if database_available else "database_unavailable",
            "database": "available" if database_available else "unavailable",
            "model_readiness": "unknown",
        }
        return JSONResponse(payload, status_code=200 if database_available else 503)

    @app.get("/v1/events")
    def event_feed(request: Request, last_event_id: str | None = Header(default=None, alias="Last-Event-ID"), scope: Scope = Depends(get_scope)) -> StreamingResponse:
        try:
            cursor = parse_last_event_id(last_event_id)
        except EventCursorError as error:
            raise HTTPException(status_code=400, detail="Invalid event cursor") from error

        def load_batch(after: int, current_scope: Scope) -> list[dict]:
            with connect() as connection:
                return read_events(connection, current_scope, after, limit=1)

        reset_event = None
        try:
            load_batch(cursor, scope)
        except EventCursorExpired as error:
            reset_event = reset_required_event(error.latest_sequence)
        except EventCursorError as error:
            raise HTTPException(status_code=400, detail="Event cursor is ahead of the stream") from error
        except EventForbidden as error:
            raise HTTPException(status_code=403, detail="Event stream access is not permitted") from error

        async def stream():
            current_cursor = cursor
            if reset_event is not None:
                try:
                    await app.state.refresh_scope(request)
                except HTTPException:
                    return
                if await request.is_disconnected():
                    return
                yield encode_sse(reset_event)
                current_cursor = reset_event["sequence"]
            while True:
                if await request.is_disconnected():
                    return
                try:
                    current_scope = await app.state.refresh_scope(request)
                    batch = await run_in_threadpool(load_batch, current_cursor, current_scope)
                except EventCursorExpired as error:
                    envelope = reset_required_event(error.latest_sequence)
                    try:
                        await app.state.refresh_scope(request)
                    except HTTPException:
                        return
                    yield encode_sse(envelope)
                    current_cursor = envelope["sequence"]
                    continue
                except (HTTPException, EventForbidden, EventCursorError):
                    return
                if batch:
                    try:
                        verified_scope = await app.state.refresh_scope(request)
                    except HTTPException:
                        return
                    if verified_scope != current_scope:
                        continue
                    if await request.is_disconnected():
                        return
                    envelope = batch[0]
                    yield encode_sse(envelope)
                    current_cursor = envelope["sequence"]
                else:
                    await asyncio.sleep(1)
                    if await request.is_disconnected():
                        return
                    yield ": keepalive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/v1/live-calls")
    def list_live_calls(scope: Scope = Depends(get_scope)) -> dict:
        try:
            with connect() as connection:
                return {"items": read_live_calls(connection, scope)}
        except LiveCallsForbidden as error:
            raise HTTPException(status_code=403, detail="Role cannot view live calls") from error

    @app.get("/v1/operations/summary")
    def get_operations_summary(scope: Scope = Depends(get_scope)) -> dict[str, int]:
        if scope.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Administrator role required")
        try:
            with connect() as connection:
                return operations_summary(connection, scope.organisation_id)
        except Exception as error:
            raise HTTPException(status_code=503, detail="Operations summary unavailable") from error

    @app.get("/v1/csrf")
    async def get_csrf_token(request: Request, response: Response, _scope: Scope = Depends(get_scope)) -> dict[str, str]:
        session_token = request.cookies.get("session")
        if not session_token or request.headers.get("Authorization"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Cookie session required")
        response.headers["Cache-Control"] = "no-store"
        return {"csrf_token": csrf_token_for_session(session_token, settings.csrf_secret)}

    @app.get("/v1/calls/{call_id}")
    def get_call(call_id: str, scope: Scope = Depends(get_scope)) -> dict:
        with connect() as connection:
            try:
                return call_detail(connection, scope, call_id)
            except ReviewNotFound as error:
                raise HTTPException(status_code=404, detail="Call not found") from error

    @app.get("/v1/reviews/queue")
    def get_review_queue(limit: int = 50, scope: Scope = Depends(get_scope)) -> dict:
        try:
            with connect() as connection:
                items = review_queue(connection, scope, limit=limit)
        except ReviewForbidden as error:
            raise HTTPException(status_code=403, detail="QA analyst permission required") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return {"items": items}

    @app.get("/v1/me/scores")
    def get_own_scores(scope: Scope = Depends(get_scope)) -> dict:
        if scope.role != "AGENT" or not scope.user_id.strip() or not scope.organisation_id.strip():
            raise HTTPException(status_code=403, detail="Agent identity required")
        try:
            with connect() as connection:
                scores = own_scores(connection, scope, redact=report_redactor)
        except ReportForbidden as error:
            raise HTTPException(status_code=403, detail="Agent identity required") from error
        except ReportPrivacyUnavailable as error:
            raise HTTPException(status_code=503, detail="Coaching notes are unavailable") from error
        return {"items": scores}

    @app.get("/v1/reports/team")
    def get_team_report(start: datetime, end: datetime, scope: Scope = Depends(get_scope)) -> dict:
        if scope.role != "TEAM_LEADER" or not scope.team_ids:
            raise HTTPException(status_code=403, detail="Team leader membership required")
        try:
            with connect() as connection:
                return team_report(connection, scope, start, end)
        except ReportForbidden as error:
            raise HTTPException(status_code=403, detail="Team leader membership required") from error
        except ReportLimitError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.get("/v1/exports/findings")
    def download_findings(start: datetime, end: datetime, request: Request, limit: int = 10_000, scope: Scope = Depends(get_scope)):
        if scope.role != "COMPLIANCE_OFFICER":
            raise HTTPException(status_code=403, detail="Compliance officer role required")
        request_id = request.headers.get("X-Request-ID")
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise HTTPException(status_code=400, detail="Invalid request identifier")
        export_id = uuid4()
        try:
            with connect() as connection:
                with connection.transaction():
                    content, row_count = export_findings(connection, scope, start, end, redact=report_redactor, limit=limit)
                    connection.execute(
                        "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome,request_id) "
                        "VALUES (%s,%s,%s,'FINDINGS_EXPORTED','FINDINGS_EXPORT',%s,'SUCCESS',%s)",
                        (scope.organisation_id, uuid4(), scope.user_id, export_id, request_id),
                    )
        except ReportForbidden as error:
            raise HTTPException(status_code=403, detail="Compliance officer role required") from error
        except ReportLimitError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except ReportPrivacyUnavailable as error:
            raise HTTPException(status_code=503, detail="Export redaction is unavailable") from error
        filename = f"findings-{start.astimezone(timezone.utc).date().isoformat()}-{end.astimezone(timezone.utc).date().isoformat()}.csv"
        return Response(content=content, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store", "X-Export-ID": str(export_id), "X-Export-Row-Count": str(row_count)})

    @app.post("/v1/calls/{call_id}/audio-access")
    def grant_audio_access(call_id: str, request: Request, scope: Scope = Depends(get_scope)) -> dict:
        try:
            canonical_call_id = str(UUID(call_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=404, detail="Call not found") from None
        if scope.role not in {"QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"}:
            raise HTTPException(status_code=403, detail="Analyst audio access required")
        request_id = request.headers.get("X-Request-ID")
        if request_id is not None and (not request_id or len(request_id) > 128):
            raise HTTPException(status_code=400, detail="Invalid request identifier")
        with connect() as connection:
            with connection.transaction():
                row = connection.execute(
                    "SELECT a.id,a.private_key,a.codec,c.agent_id,c.team_id "
                    "FROM calls c JOIN audio_objects a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
                    "WHERE c.organisation_id=%s AND c.id=%s AND c.tombstoned_at IS NULL",
                    (scope.organisation_id, canonical_call_id),
                ).fetchone()
                if row is None or not can_access(scope, scope.organisation_id, row[3], row[4]):
                    raise HTTPException(status_code=404, detail="Call not found")
                connection.execute(
                    "INSERT INTO access_events(organisation_id,id,actor_id,action,resource_type,resource_id,outcome,request_id) "
                    "VALUES (%s,%s,%s,'AUDIO_ACCESS_GRANTED','AUDIO_OBJECT',%s,'SUCCESS',%s)",
                    (scope.organisation_id, uuid4(), scope.user_id, row[0], request_id),
                )
        capability = issue_audio_capability(scope, canonical_call_id, str(row[0]), settings.csrf_secret)
        return {"url": f"/v1/calls/{canonical_call_id}/audio?capability={capability}", "expires_in_seconds": 60}

    @app.get("/v1/calls/{call_id}/audio")
    def stream_call_audio(call_id: str, capability: str, request: Request, scope: Scope = Depends(get_scope)):
        try:
            canonical_call_id = str(UUID(call_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=404, detail="Call not found") from None
        audio_id = verify_audio_capability(capability, scope, canonical_call_id, settings.csrf_secret)
        if audio_id is None:
            raise HTTPException(status_code=403, detail="Audio capability is invalid or expired")
        if scope.role not in {"QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"}:
            raise HTTPException(status_code=403, detail="Analyst audio access required")
        with connect() as connection:
            row = connection.execute(
                "SELECT a.private_key,a.codec,c.agent_id,c.team_id,c.tombstoned_at "
                "FROM calls c JOIN audio_objects a ON a.organisation_id=c.organisation_id AND a.call_id=c.id "
                "WHERE c.organisation_id=%s AND c.id=%s AND a.id=%s",
                (scope.organisation_id, canonical_call_id, audio_id),
            ).fetchone()
        if row is None or row[4] is not None or not can_access(scope, scope.organisation_id, row[2], row[3]):
            raise HTTPException(status_code=404, detail="Call not found")
        storage = LocalPrivateStorage(os.environ.get("AUDIO_STORAGE_PATH", "./private-audio"))
        try:
            size = storage.size(row[0], max_bytes=MAX_AUDIO_BYTES)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="Audio is unavailable") from None
        if size == 0:
            raise HTTPException(status_code=404, detail="Audio is unavailable")
        start, end, status_code = 0, size - 1, 200
        headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, no-store", "Content-Length": str(size)}
        range_header = request.headers.get("Range")
        if range_header:
            if len(range_header) > 128:
                raise HTTPException(status_code=416, detail="Invalid byte range", headers={"Content-Range": f"bytes */{size}"})
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or (not match.group(1) and not match.group(2)):
                raise HTTPException(status_code=416, detail="Invalid byte range", headers={"Content-Range": f"bytes */{size}"})
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                suffix = int(match.group(2))
                start = max(0, size - suffix)
                end = size - 1
            if start >= size or end < start:
                raise HTTPException(status_code=416, detail="Range is not satisfiable", headers={"Content-Range": f"bytes */{size}"})
            end = min(end, size - 1)
            status_code = 206
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            headers["Content-Length"] = str(end - start + 1)
        content_type = "audio/mpeg" if row[1] == "mp3" else "audio/wav"
        return StreamingResponse(storage.iter_range(row[0], start, end), status_code=status_code, media_type=content_type, headers=headers)

    @app.post("/v1/audits/{audit_id}/reviews", status_code=201)
    def create_review(audit_id: str, body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        if set(body) != {"action", "base_review_version", "scores", "reason"}:
            raise HTTPException(status_code=422, detail="Review body fields are invalid")
        try:
            with connect() as connection:
                review = append_review(
                    connection, scope, audit_id, body["base_review_version"], body["action"], body["scores"], body["reason"],
                    request_id=request.headers.get("X-Request-ID") if request else None,
                )
        except ReviewConflict as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except ReviewNotFound as error:
            raise HTTPException(status_code=404, detail="Audit not found") from error
        except ReviewForbidden as error:
            raise HTTPException(status_code=403, detail="QA analyst permission required") from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return review

    @app.get("/v1/calls/{call_id}/disposition")
    def get_disposition(call_id: str, scope: Scope = Depends(get_scope)) -> dict:
        with connect() as connection:
            call = connection.execute("SELECT id,organisation_id,agent_id,team_id FROM calls WHERE organisation_id=%s AND id=%s AND tombstoned_at IS NULL", (scope.organisation_id, call_id)).fetchone()
            if call is None or not can_access(scope, str(call[1]), call[2], call[3]):
                raise HTTPException(status_code=404)
            row = connection.execute(
                "SELECT revision,transcript_revision,config_id,config_version,config_hash,schema_version,model_artifact,adapter_version,processing_path,status,code,parent_code,confidence,requires_review,review_reason,matched_rule_id,signals_json,usage_json,created_at "
                "FROM dispositions WHERE organisation_id=%s AND call_id=%s ORDER BY revision DESC LIMIT 1",
                (scope.organisation_id, call_id),
            ).fetchone()
        if row is None:
            return {"call_id": call_id, "status": "PENDING"}
        return {"call_id": call_id, "revision": row[0], "transcript_revision": row[1], "config_id": row[2], "config_version": row[3], "config_hash": row[4].strip(), "schema_version": row[5], "model_artifact": row[6], "adapter_version": row[7], "processing_path": row[8], "status": row[9], "code": row[10], "parent_code": row[11], "confidence": row[12], "requires_review": row[13], "review_reason": row[14], "matched_rule_id": row[15], "signals": row[16], "usage": row[17], "created_at": row[18].isoformat()}

    @app.delete("/v1/calls/{call_id}", status_code=202)
    def delete_call(call_id: str, scope: Scope = Depends(get_scope)) -> dict:
        if scope.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Administrator role required")
        try:
            canonical_call_id = str(UUID(call_id))
        except (ValueError, TypeError, AttributeError):
            raise HTTPException(status_code=404, detail="Call not found") from None
        with connect() as connection:
            result = request_call_deletion(connection, scope.organisation_id, canonical_call_id, scope.user_id)
        if result["status"] == "NOT_FOUND":
            raise HTTPException(status_code=404, detail="Call not found")
        if result["status"] == "HELD":
            raise HTTPException(status_code=409, detail="Call is under legal hold")
        return {"call_id": canonical_call_id, **result}

    def require_admin(scope: Scope) -> None:
        if scope.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Administrator role required")

    def write_config_event(connection, scope: Scope, action: str, config_id: str, version: int, *, reason: str | None = None, request_id: str | None = None, details: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO disposition_config_events(organisation_id,id,actor_id,action,config_id,version,reason,request_id,details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (scope.organisation_id, uuid4(), scope.user_id, action, config_id, version, reason, request_id, json.dumps(details or {})),
        )

    def write_exotel_event(connection, scope: Scope, integration_id: str, action: str, *, subject_ref: str | None = None, request_id: str | None = None) -> None:
        connection.execute(
            "INSERT INTO exotel_integration_events(organisation_id,integration_id,actor_id,action,subject_ref,request_id) VALUES (%s,%s,%s,%s,%s,%s)",
            (scope.organisation_id, integration_id, scope.user_id, action, subject_ref, request_id),
        )

    @app.post("/v1/exotel-integrations", status_code=201)
    def create_exotel_integration(body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        account_sid = body.get("account_sid")
        if set(body) != {"account_sid"} or not isinstance(account_sid, str) or not account_sid.strip() or len(account_sid) > 256:
            raise HTTPException(status_code=422, detail="A bounded Exotel account SID is required")
        account_sid = account_sid.strip()
        integration_id = uuid4()
        username, password, salt, verifier = integration_credentials()
        try:
            with connect() as connection:
                with connection.transaction():
                    connection.execute("INSERT INTO exotel_integrations(organisation_id,id,account_sid,username,password_salt,password_verifier,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)", (scope.organisation_id, integration_id, account_sid, username, salt, verifier, scope.user_id))
                    write_exotel_event(connection, scope, str(integration_id), "CREATED", request_id=request.headers.get("X-Request-ID") if request else None)
        except UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Exotel account is already configured") from error
        return {"id": str(integration_id), "account_sid": account_sid, "username": username, "password": password, "status": "ACTIVE"}

    @app.get("/v1/exotel-integrations")
    def list_exotel_integrations(scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        with connect() as connection:
            rows = connection.execute("SELECT id,account_sid,username,is_active,created_at FROM exotel_integrations WHERE organisation_id=%s ORDER BY created_at,id", (scope.organisation_id,)).fetchall()
        return {"integrations": [{"id": str(row[0]), "account_sid": row[1], "username": row[2], "status": "ACTIVE" if row[3] else "DISABLED", "created_at": row[4].isoformat()} for row in rows]}

    @app.delete("/v1/exotel-integrations/{integration_id}", status_code=204)
    def disable_exotel_integration(integration_id: str, request: Request, scope: Scope = Depends(get_scope)) -> Response:
        require_admin(scope)
        try:
            integration_id = str(UUID(integration_id))
        except ValueError as error:
            raise HTTPException(status_code=404) from error
        with connect() as connection:
            with connection.transaction():
                changed = connection.execute("UPDATE exotel_integrations SET is_active=false,disabled_at=now() WHERE organisation_id=%s AND id=%s AND is_active RETURNING id", (scope.organisation_id, integration_id)).fetchone()
                if changed is None:
                    exists = connection.execute("SELECT 1 FROM exotel_integrations WHERE organisation_id=%s AND id=%s", (scope.organisation_id, integration_id)).fetchone()
                    if exists is None:
                        raise HTTPException(status_code=404)
                else:
                    write_exotel_event(connection, scope, integration_id, "DISABLED", request_id=request.headers.get("X-Request-ID"))
        return Response(status_code=204)

    @app.get("/v1/exotel-integrations/{integration_id}/agents")
    def list_exotel_agents(integration_id: str, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        try:
            integration_id = str(UUID(integration_id))
        except ValueError as error:
            raise HTTPException(status_code=404) from error
        with connect() as connection:
            rows = connection.execute("SELECT agent_ref,agent_id,team_id,created_by,created_at FROM exotel_agent_mappings WHERE organisation_id=%s AND integration_id=%s ORDER BY agent_ref", (scope.organisation_id, integration_id)).fetchall()
        return {"mappings": [{"agent_ref": row[0], "agent_id": row[1], "team_id": row[2], "created_by": row[3], "created_at": row[4].isoformat()} for row in rows]}

    @app.put("/v1/exotel-integrations/{integration_id}/agents/{agent_ref}")
    def map_exotel_agent(integration_id: str, agent_ref: str, body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        if len(agent_ref) > 128 or not agent_ref.strip() or set(body) != {"agent_id", "team_id"}:
            raise HTTPException(status_code=422, detail="A bounded agent reference, agent ID, and team ID are required")
        agent_id, team_id = body.get("agent_id"), body.get("team_id")
        if not all(isinstance(value, str) and value.strip() and len(value) <= 256 for value in (agent_id, team_id)):
            raise HTTPException(status_code=422, detail="Invalid agent mapping")
        agent_id, team_id = agent_id.strip(), team_id.strip()
        try:
            integration_id = str(UUID(integration_id))
        except ValueError as error:
            raise HTTPException(status_code=404) from error
        with connect() as connection:
            with connection.transaction():
                active = connection.execute("SELECT 1 FROM exotel_integrations WHERE organisation_id=%s AND id=%s AND is_active", (scope.organisation_id, integration_id)).fetchone()
                if active is None:
                    raise HTTPException(status_code=404)
                memberships = connection.execute("SELECT team_id FROM identity_memberships WHERE organisation_id=%s AND user_id=%s AND role='AGENT' AND is_active ORDER BY team_id", (scope.organisation_id, agent_id)).fetchall()
                if len(memberships) != 1 or memberships[0][0] != team_id:
                    raise HTTPException(status_code=422, detail="Agent must have exactly one active AGENT team membership matching the mapping")
                connection.execute("INSERT INTO exotel_agent_mappings(organisation_id,integration_id,agent_ref,agent_id,team_id,created_by) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (organisation_id,integration_id,agent_ref) DO UPDATE SET agent_id=EXCLUDED.agent_id,team_id=EXCLUDED.team_id,created_by=EXCLUDED.created_by,created_at=now()", (scope.organisation_id, integration_id, agent_ref, agent_id, team_id, scope.user_id))
                write_exotel_event(connection, scope, integration_id, "AGENT_MAPPED", subject_ref=agent_ref, request_id=request.headers.get("X-Request-ID") if request else None)
        return {"agent_ref": agent_ref, "agent_id": agent_id, "team_id": team_id}

    @app.delete("/v1/exotel-integrations/{integration_id}/agents/{agent_ref}", status_code=204)
    def unmap_exotel_agent(integration_id: str, agent_ref: str, request: Request, scope: Scope = Depends(get_scope)) -> Response:
        require_admin(scope)
        try:
            integration_id = str(UUID(integration_id))
        except ValueError as error:
            raise HTTPException(status_code=404) from error
        with connect() as connection:
            with connection.transaction():
                deleted = connection.execute("DELETE FROM exotel_agent_mappings WHERE organisation_id=%s AND integration_id=%s AND agent_ref=%s RETURNING agent_ref", (scope.organisation_id, integration_id, agent_ref)).fetchone()
                if deleted is not None:
                    write_exotel_event(connection, scope, integration_id, "AGENT_UNMAPPED", subject_ref=agent_ref, request_id=request.headers.get("X-Request-ID"))
        return Response(status_code=204)

    def authenticate_exotel(header: str | None):
        credentials = parse_basic_authorization(header)
        if credentials is None:
            return None
        username, password = credentials
        with connect(timeout_seconds=5) as connection:
            row = connection.execute("SELECT organisation_id::text,id::text,account_sid,password_salt,password_verifier FROM exotel_integrations WHERE username=%s AND is_active", (username,)).fetchone()
        # A fixed dummy verifier makes unknown usernames take the same password-check path.
        salt = row[3] if row else bytes(16)
        verifier = row[4] if row else bytes(32)
        valid = verify_integration_secret(password, salt, verifier)
        return row if row is not None and valid else None

    def resolve_exotel_agent(organisation_id: str, integration_id: str, agent_ref: str):
        with connect(timeout_seconds=5) as connection:
            row = connection.execute("SELECT agent_id,team_id FROM exotel_agent_mappings WHERE organisation_id=%s AND integration_id=%s AND agent_ref=%s", (organisation_id, integration_id, agent_ref)).fetchone()
            if row is None:
                return None
            memberships = connection.execute("SELECT team_id FROM identity_memberships WHERE organisation_id=%s AND user_id=%s AND role='AGENT' AND is_active ORDER BY team_id", (organisation_id, row[0])).fetchall()
            if len(memberships) != 1 or memberships[0][0] != row[1]:
                return None
            return Scope(organisation_id, row[0], "AGENT", frozenset({row[1]}))

    def next_exotel_generation(organisation_id: str, integration_id: str, call_key: str,
                               agent_ref: str, agent_id: str, team_id: str) -> int:
        with connect(timeout_seconds=5) as connection:
            return activate_exotel_session(connection, organisation_id, integration_id, call_key, agent_ref, agent_id, team_id)

    def persist_exotel_state(organisation_id: str, integration_id: str, call_key: str, generation: int,
                             state: str, *, activity: bool = False) -> bool:
        with connect(timeout_seconds=5) as connection:
            with connection.transaction():
                return update_exotel_session(connection, organisation_id, integration_id, call_key, generation, state, activity=activity)

    @app.websocket("/v1/exotel/stream")
    async def exotel_stream(websocket: WebSocket) -> None:
        if not _EXOTEL_CONNECTIONS.acquire(blocking=False):
            await websocket.close(code=4429)
            return
        loop = asyncio.get_running_loop()
        session_deadline = loop.time() + EXOTEL_SESSION_TIMEOUT_SECONDS
        handshake_deadline = min(session_deadline, loop.time() + EXOTEL_HANDSHAKE_TIMEOUT_SECONDS)
        lifecycle = None

        async def receive_text_before(deadline: float, max_wait: float) -> str:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError
            try:
                return await asyncio.wait_for(websocket.receive_text(), timeout=min(remaining, max_wait))
            except asyncio.TimeoutError as error:
                raise TimeoutError from error

        try:
            try:
                integration = await run_in_threadpool(authenticate_exotel, websocket.headers.get("authorization"))
            except Exception:
                integration = None
            if integration is None:
                await websocket.close(code=4401)
                return
            await websocket.accept()
            connected = await receive_text_before(handshake_deadline, EXOTEL_HANDSHAKE_TIMEOUT_SECONDS)
            start_raw = await receive_text_before(handshake_deadline, EXOTEL_HANDSHAKE_TIMEOUT_SECONDS)
            try:
                preliminary = ExotelSession(account_sid=integration[2], call_sid="pending", generation=1)
                preliminary.accept(connected, generation=1)
                # Read only identity and custom mapping reference from start; parser validates all fields below.
                start_event = json.loads(start_raw)
                start_body = start_event.get("start", {}) if isinstance(start_event, dict) else {}
                call_sid = start_body.get("call_sid")
                custom = start_body.get("custom_parameters", {})
                agent_ref = custom.get("agent_ref") if isinstance(custom, dict) else None
                if not isinstance(call_sid, str) or not isinstance(agent_ref, str):
                    raise ExotelProtocolError("required call mapping is missing")
                preliminary.call_sid = call_sid
                validated_start = preliminary.accept(start_raw, generation=1)
                if not isinstance(validated_start, ExotelLifecycleEvent) or validated_start.missing_sequences:
                    raise ExotelProtocolError("incomplete stream sequence")
                agent_scope = await run_in_threadpool(resolve_exotel_agent, integration[0], integration[1], agent_ref)
                if agent_scope is None:
                    raise ExotelProtocolError("call agent mapping is unavailable")
                external_ref, idempotency_key = make_audio_references(integration[0], integration[2], call_sid)
                call_key = external_ref.partition(":")[2]
                agent_id = agent_scope.user_id
                team_id = next(iter(agent_scope.team_ids))
                generation = await run_in_threadpool(next_exotel_generation, integration[0], integration[1], call_key, agent_ref, agent_id, team_id)
                lifecycle = (integration[0], integration[1], call_key, generation)
                session = ExotelSession(account_sid=integration[2], call_sid=call_sid, generation=generation)
                session.accept(connected, generation=generation)
                started = session.accept(start_raw, generation=generation)
                if not isinstance(started, ExotelLifecycleEvent) or started.missing_sequences:
                    raise ExotelProtocolError("incomplete stream sequence")
            except (ExotelProtocolError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                await websocket.close(code=4400)
                return

            total_pcm_bytes = 0
            expected_timestamp = 0
            last_activity_update = loop.time()
            with tempfile.SpooledTemporaryFile(max_size=2 * 1024 * 1024) as spool:
                while True:
                    raw = await receive_text_before(session_deadline, EXOTEL_MEDIA_IDLE_TIMEOUT_SECONDS)
                    try:
                        event = session.accept(raw, generation=generation)
                    except ExotelProtocolError:
                        await websocket.close(code=4400)
                        return
                    if isinstance(event, ExotelLifecycleEvent):
                        if event.missing_sequences or event.missing_chunks:
                            await websocket.close(code=4400)
                            return
                        if event.event == "stop":
                            await run_in_threadpool(persist_exotel_state, *lifecycle, "DRAINING")
                            break
                    elif event is not None:
                        if event.missing_sequences or event.missing_chunks or event.stream_offset_ms != expected_timestamp:
                            await websocket.close(code=4400)
                            return
                        expected_timestamp += len(event.pcm) // 16
                        total_pcm_bytes += len(event.pcm)
                        if expected_timestamp > ExotelSession.MAX_DURATION_MS or total_pcm_bytes > ExotelSession.MAX_STREAM_BYTES:
                            await websocket.close(code=4400)
                            return
                        spool.write(event.pcm)
                        if loop.time() - last_activity_update >= 15:
                            await run_in_threadpool(persist_exotel_state, *lifecycle, "LIVE", activity=True)
                            last_activity_update = loop.time()
                if not total_pcm_bytes:
                    await websocket.close(code=4400)
                    return
                spool.seek(0)
                audio = build_wav(spool.read())
            try:
                remaining_seconds = session_deadline - loop.time()
                if remaining_seconds <= 0:
                    await websocket.close(code=4408)
                    return
                await run_in_threadpool(
                    accept_recording, agent_scope, external_ref, audio, {"language": "und"}, idempotency_key,
                    generation_fence=(integration[1], call_key, generation, agent_ref, agent_scope.user_id, next(iter(agent_scope.team_ids))),
                    timeout_seconds=remaining_seconds,
                )
            except (IntakeError, ExternalReferenceConflict, IdempotencyConflict):
                await websocket.close(code=4409)
                return
            if not await run_in_threadpool(persist_exotel_state, *lifecycle, "ENDED"):
                await websocket.close(code=4409)
                return
            lifecycle = None
            await websocket.close(code=1000)
        except TimeoutError:
            try:
                await websocket.close(code=4408)
            except Exception:
                pass
        except WebSocketDisconnect:
            return
        except Exception:
            try:
                await websocket.close(code=1011)
            except Exception:
                pass
        finally:
            if lifecycle is not None:
                try:
                    await run_in_threadpool(persist_exotel_state, *lifecycle, "INCOMPLETE")
                except Exception:
                    pass
            _EXOTEL_CONNECTIONS.release()

    @app.get("/v1/disposition-configs")
    def list_disposition_configs(scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        with connect() as connection:
            rows = connection.execute("SELECT v.config_id,v.use_case_id,v.version,v.content_hash,v.schema_version,v.created_by,v.created_at,a.version,a.generation,EXISTS (SELECT 1 FROM disposition_config_events e WHERE e.organisation_id=v.organisation_id AND e.config_id=v.config_id AND e.version=v.version AND e.action IN ('ACTIVATED','ROLLBACK')), (SELECT e.actor_id FROM disposition_config_events e WHERE e.organisation_id=v.organisation_id AND e.config_id=v.config_id AND e.version=v.version AND e.action='APPROVED' AND e.actor_id<>v.created_by ORDER BY e.created_at LIMIT 1) FROM disposition_config_versions v LEFT JOIN active_disposition_configs a ON a.organisation_id=v.organisation_id AND a.config_id=v.config_id WHERE v.organisation_id=%s ORDER BY v.config_id,v.version", (scope.organisation_id,)).fetchall()
        return {"versions": [{"config_id": r[0], "use_case_id": r[1], "version": r[2], "content_hash": r[3].strip(), "schema_version": r[4], "created_by": r[5], "created_at": r[6].isoformat(), "active": r[7] == r[2], "status": "ACTIVE" if r[7] == r[2] else ("RETIRED" if r[9] else ("APPROVED" if r[10] else "STAGED")), "approved_by": r[10], "active_generation": r[8]} for r in rows]}

    @app.post("/v1/disposition-configs/validate")
    def validate_disposition_config(raw: dict = Body(...), scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        try:
            compiled = compile_disposition_config(raw)
        except ConfigError as error:
            raise HTTPException(status_code=422, detail=error.errors) from error
        return {"valid": True, "config_id": compiled.config_id, "use_case_id": compiled.use_case_id, "version": compiled.version, "content_hash": compiled.content_hash, "schema_version": compiled.schema_version, "question_count": len(compiled.questions), "taxonomy_codes": sorted(compiled.taxonomy)}

    @app.post("/v1/disposition-configs", status_code=201)
    def stage_disposition_config(raw: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        try:
            compiled = compile_disposition_config(raw)
        except ConfigError as error:
            raise HTTPException(status_code=422, detail=error.errors) from error
        try:
            with connect() as connection:
                with connection.transaction():
                    connection.execute("INSERT INTO disposition_config_versions(organisation_id,config_id,use_case_id,version,content_hash,schema_version,raw_json,created_by) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s)", (scope.organisation_id, compiled.config_id, compiled.use_case_id, compiled.version, compiled.content_hash, compiled.schema_version, json.dumps(compiled.normalized), scope.user_id))
                    write_config_event(connection, scope, "STAGED", compiled.config_id, compiled.version, request_id=request.headers.get("X-Request-ID") if request else None)
        except UniqueViolation as error:
            raise HTTPException(status_code=409, detail="Configuration version already exists or conflicts") from error
        return {"config_id": compiled.config_id, "use_case_id": compiled.use_case_id, "version": compiled.version, "content_hash": compiled.content_hash, "status": "STAGED"}

    @app.post("/v1/disposition-configs/{config_id}/versions/{version}/approve")
    def approve_disposition_config(config_id: str, version: int, body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        reason = body.get("reason")
        if set(body) != {"reason"} or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise HTTPException(status_code=422, detail="A bounded approval reason is required")
        with connect() as connection:
            with connection.transaction():
                row = connection.execute("SELECT created_by FROM disposition_config_versions WHERE organisation_id=%s AND config_id=%s AND version=%s", (scope.organisation_id, config_id, version)).fetchone()
                if row is None:
                    raise HTTPException(status_code=404)
                if row[0] == scope.user_id:
                    raise HTTPException(status_code=403, detail="A different administrator must approve this version")
                existing = connection.execute("SELECT 1 FROM disposition_config_events WHERE organisation_id=%s AND config_id=%s AND version=%s AND action='APPROVED' AND actor_id<>%s LIMIT 1", (scope.organisation_id, config_id, version, row[0])).fetchone()
                if existing:
                    raise HTTPException(status_code=409, detail="Configuration version is already approved")
                write_config_event(connection, scope, "APPROVED", config_id, version, reason=reason.strip(), request_id=request.headers.get("X-Request-ID") if request else None)
        return {"config_id": config_id, "version": version, "approved_by": scope.user_id, "status": "APPROVED"}

    @app.post("/v1/disposition-configs/{config_id}/versions/{version}/activate")
    def activate_disposition_config(config_id: str, version: int, body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        expected = body.get("expected_generation")
        if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
            raise HTTPException(status_code=422, detail="expected_generation must be a nonnegative integer")
        with connect() as connection:
            with connection.transaction():
                connection.execute("SELECT id FROM organisations WHERE id=%s FOR UPDATE", (scope.organisation_id,))
                config = connection.execute("SELECT created_by FROM disposition_config_versions WHERE organisation_id=%s AND config_id=%s AND version=%s", (scope.organisation_id, config_id, version)).fetchone()
                if config is None: raise HTTPException(status_code=404)
                approval = connection.execute("SELECT actor_id FROM disposition_config_events WHERE organisation_id=%s AND config_id=%s AND version=%s AND action='APPROVED' AND actor_id<>%s ORDER BY created_at LIMIT 1", (scope.organisation_id, config_id, version, config[0])).fetchone()
                if approval is None: raise HTTPException(status_code=409, detail="Configuration version requires approval by a different administrator")
                current = connection.execute("SELECT config_id,version,generation FROM active_disposition_configs WHERE organisation_id=%s FOR UPDATE", (scope.organisation_id,)).fetchone()
                actual = current[2] if current else 0
                if actual != expected: raise HTTPException(status_code=409, detail="Active configuration changed")
                generation = actual + 1
                if current:
                    connection.execute("UPDATE active_disposition_configs SET config_id=%s,version=%s,generation=%s,changed_by=%s,changed_at=now() WHERE organisation_id=%s AND generation=%s", (config_id, version, generation, scope.user_id, scope.organisation_id, expected))
                else:
                    connection.execute("INSERT INTO active_disposition_configs(organisation_id,config_id,version,generation,changed_by) VALUES (%s,%s,%s,%s,%s)", (scope.organisation_id, config_id, version, generation, scope.user_id))
                write_config_event(connection, scope, "ACTIVATED", config_id, version, request_id=request.headers.get("X-Request-ID") if request else None, details={"generation": generation})
        return {"config_id": config_id, "version": version, "generation": generation}

    @app.post("/v1/disposition-configs/{config_id}/versions/{version}/replay")
    def replay_disposition_config(config_id: str, version: int, body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        if set(body) != {"fixture_set_id"} or body.get("fixture_set_id") != "synthetic-smoke-v1":
            raise HTTPException(status_code=422, detail="Only the bundled synthetic-smoke-v1 fixture set is supported")
        if disposition_adapter is None:
            raise HTTPException(status_code=503, detail="Local disposition model is unavailable")
        with connect() as connection:
            row = connection.execute("SELECT raw_json FROM disposition_config_versions WHERE organisation_id=%s AND config_id=%s AND version=%s", (scope.organisation_id, config_id, version)).fetchone()
            if row is None: raise HTTPException(status_code=404)
        compiled = compile_disposition_config(row[0])
        fixtures = [{"id": "synthetic-customer-1", "role": "CUSTOMER", "start_ms": 0, "end_ms": 1000, "text_redacted": "Synthetic customer asks about a sample service."}, {"id": "synthetic-agent-1", "role": "AGENT", "start_ms": 1001, "end_ms": 2000, "text_redacted": "Synthetic agent offers a sample next step."}]
        result = classify_disposition(fixtures, {}, disposition_adapter, compiled)
        with connect() as connection:
            with connection.transaction():
                write_config_event(connection, scope, "REPLAYED", config_id, version, request_id=request.headers.get("X-Request-ID") if request else None, details={"fixture_set_id": "synthetic-smoke-v1", "status": result.status})
        return {"fixture_set_id": "synthetic-smoke-v1", "sample_count": 1, "resolved_count": int(result.status == "RESOLVED"), "review_count": int(result.status == "NEEDS_REVIEW"), "results": [{"status": result.status, "code": result.code, "requires_review": result.requires_review}]}

    @app.post("/v1/disposition-configs/{config_id}/rollback")
    def rollback_disposition_config(config_id: str, body: dict = Body(...), request: Request = None, scope: Scope = Depends(get_scope)) -> dict:
        require_admin(scope)
        target_version, expected, reason = body.get("version"), body.get("expected_generation"), body.get("reason")
        if isinstance(target_version, bool) or not isinstance(target_version, int) or target_version < 1 or isinstance(expected, bool) or not isinstance(expected, int) or expected < 1 or not isinstance(reason, str) or not reason.strip() or len(reason) > 1000:
            raise HTTPException(status_code=422, detail="version, expected_generation, and reason are required")
        with connect() as connection:
            with connection.transaction():
                connection.execute("SELECT id FROM organisations WHERE id=%s FOR UPDATE", (scope.organisation_id,))
                prior = connection.execute("SELECT 1 FROM disposition_config_events e JOIN disposition_config_versions v ON v.organisation_id=e.organisation_id AND v.config_id=e.config_id AND v.version=e.version WHERE e.organisation_id=%s AND e.config_id=%s AND e.version=%s AND e.action IN ('ACTIVATED','ROLLBACK') AND EXISTS (SELECT 1 FROM disposition_config_events approved WHERE approved.organisation_id=e.organisation_id AND approved.config_id=e.config_id AND approved.version=e.version AND approved.action='APPROVED' AND approved.actor_id<>v.created_by) LIMIT 1", (scope.organisation_id, config_id, target_version)).fetchone()
                if prior is None: raise HTTPException(status_code=409, detail="Rollback target must be previously active and independently approved")
                current = connection.execute("SELECT config_id,version,generation FROM active_disposition_configs WHERE organisation_id=%s FOR UPDATE", (scope.organisation_id,)).fetchone()
                if current is None or current[2] != expected: raise HTTPException(status_code=409, detail="Active configuration changed")
                generation = expected + 1
                connection.execute("UPDATE active_disposition_configs SET config_id=%s,version=%s,generation=%s,changed_by=%s,changed_at=now() WHERE organisation_id=%s AND generation=%s", (config_id, target_version, generation, scope.user_id, scope.organisation_id, expected))
                write_config_event(connection, scope, "ROLLBACK", config_id, target_version, reason=reason.strip(), request_id=request.headers.get("X-Request-ID") if request else None, details={"generation": generation})
        return {"config_id": config_id, "version": target_version, "generation": generation, "status": "ROLLED_BACK"}

    @app.post("/v1/calls", status_code=status.HTTP_202_ACCEPTED)
    async def upload_call(
        audio: UploadFile = File(...),
        external_ref: str = Form(...),
        language: str = Form("und", max_length=32),
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
        except ExternalReferenceConflict as error:
            raise HTTPException(status_code=409, detail="External reference already exists") from error
        except IdempotencyConflict as error:
            raise HTTPException(status_code=409, detail="Idempotency key conflicts with existing upload") from error
        except IntakeError as error:
            raise HTTPException(status_code=400, detail="Invalid audio") from error
        return result

    web_root = Path(__file__).resolve().parent.parent / "web"
    if web_root.is_dir():
        @app.get("/analyst", include_in_schema=False)
        def analyst_home():
            return FileResponse(web_root / "index.html")

        @app.get("/quality", include_in_schema=False)
        def quality_home():
            return FileResponse(web_root / "quality.html")

        @app.get("/providers", include_in_schema=False)
        def providers_home():
            return FileResponse(web_root / "providers.html")

        app.mount("/analyst-assets", StaticFiles(directory=web_root), name="analyst-assets")

    limited_app = UploadBodyLimitMiddleware(app, max_bytes=MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES, get_scope=get_scope)
    limited_app = DispositionBodyLimitMiddleware(limited_app, get_scope=get_scope)
    limited_app = ReviewBodyLimitMiddleware(limited_app, get_scope=get_scope)
    return CORSMiddleware(
        limited_app,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Organisation-ID", "X-Request-ID"],
    )
