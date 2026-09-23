import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile, status
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
from app.ingest import IdempotencyConflict, IntakeError, MAX_AUDIO_BYTES, accept_recording
from app.disposition_config import ConfigError, compile_disposition_config
from app.disposition import classify_disposition
from app.local_disposition_adapter import LocalVllmDispositionAdapter
from app.reviews import ReviewConflict, ReviewForbidden, ReviewNotFound, append_review, call_detail, review_queue
from app.audio_access import issue_audio_capability, verify_audio_capability
from app.storage import LocalPrivateStorage
from app.privacy import redact_text
from app.reports import ReportForbidden, ReportLimitError, ReportPrivacyUnavailable, export_findings, own_scores, team_report
from psycopg.errors import UniqueViolation

MULTIPART_OVERHEAD_BYTES = 64 * 1024
MAX_DISPOSITION_CONFIG_BYTES = 1024 * 1024
MAX_REVIEW_BODY_BYTES = 32 * 1024


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
    report_redactor = report_redactor or redact_text
    if disposition_adapter is None and os.environ.get("DISPOSITION_MODEL_SHA256"):
        disposition_adapter = LocalVllmDispositionAdapter.from_environment()

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

    def require_admin(scope: Scope) -> None:
        if scope.role != "ADMIN":
            raise HTTPException(status_code=403, detail="Administrator role required")

    def write_config_event(connection, scope: Scope, action: str, config_id: str, version: int, *, reason: str | None = None, request_id: str | None = None, details: dict | None = None) -> None:
        connection.execute(
            "INSERT INTO disposition_config_events(organisation_id,id,actor_id,action,config_id,version,reason,request_id,details) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (scope.organisation_id, uuid4(), scope.user_id, action, config_id, version, reason, request_id, json.dumps(details or {})),
        )

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

    web_root = Path(__file__).resolve().parent.parent / "web"
    if web_root.is_dir():
        @app.get("/analyst", include_in_schema=False)
        def analyst_home():
            return FileResponse(web_root / "index.html")

        @app.get("/quality", include_in_schema=False)
        def quality_home():
            return FileResponse(web_root / "quality.html")

        app.mount("/analyst-assets", StaticFiles(directory=web_root), name="analyst-assets")

    limited_app = UploadBodyLimitMiddleware(app, max_bytes=MAX_AUDIO_BYTES + MULTIPART_OVERHEAD_BYTES, get_scope=get_scope)
    limited_app = DispositionBodyLimitMiddleware(limited_app, get_scope=get_scope)
    limited_app = ReviewBodyLimitMiddleware(limited_app, get_scope=get_scope)
    return CORSMiddleware(
        limited_app,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Organisation-ID", "X-Request-ID"],
    )
