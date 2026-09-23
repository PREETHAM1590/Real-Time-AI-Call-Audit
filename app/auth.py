import base64
import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

import jwt
from fastapi import HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.db import connect


@dataclass(frozen=True, slots=True)
class Scope:
    organisation_id: str
    user_id: str
    role: str
    team_ids: frozenset[str]


def can_access(scope: Scope, organisation_id: str, agent_id: str, team_id: str) -> bool:
    if scope.organisation_id != organisation_id:
        return False
    if scope.role == "AGENT":
        return scope.user_id == agent_id
    if scope.role == "TEAM_LEADER":
        return team_id in scope.team_ids
    return scope.role in {"QA_ANALYST", "COMPLIANCE_OFFICER", "ADMIN"}


class IdentityStoreError(RuntimeError):
    """The authoritative identity store could not be consulted."""


def resolve_identity_from_db(subject: str, organisation_id: str | None = None) -> Scope | None:
    """Resolve verified OIDC subjects only through active server-owned memberships."""
    try:
        with connect() as connection:
            if organisation_id is None:
                rows = connection.execute(
                    "SELECT organisation_id::text,user_id,role,team_id FROM identity_memberships WHERE subject=%s AND is_active ORDER BY organisation_id,user_id,role,team_id",
                    (subject,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT organisation_id::text,user_id,role,team_id FROM identity_memberships WHERE subject=%s AND organisation_id=%s::uuid AND is_active ORDER BY organisation_id,user_id,role,team_id",
                    (subject, organisation_id),
                ).fetchall()
    except Exception as error:
        raise IdentityStoreError("Identity store unavailable") from error

    if not rows:
        return None
    organisations = {row[0] for row in rows}
    if len(organisations) != 1:
        return None
    identities = {(row[1], row[2]) for row in rows}
    if len(identities) != 1:
        return None
    user_id, role = next(iter(identities))
    team_ids = frozenset(row[3] for row in rows if row[3] is not None)
    if role in {"AGENT", "TEAM_LEADER"} and not team_ids:
        return None
    if role == "AGENT" and len(team_ids) != 1:
        return None
    return Scope(next(iter(organisations)), user_id, role, team_ids)


IdentityLookup = Callable[[str], Scope | None]


def csrf_token_for_session(session_token: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def scope_dependency(settings: Settings, identity_lookup: IdentityLookup | None = None):
    """Validate an OIDC token and resolve its authority from the identity store."""

    async def get_scope(request: Request) -> Scope:
        cached = request.scope.get("state", {}).get("auth_scope")
        if cached is not None:
            return cached

        authorization = request.headers.get("Authorization", "")
        if authorization:
            if not authorization.startswith("Bearer ") or not authorization[7:]:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
            token, cookie_authenticated = authorization[7:], False
        else:
            token = request.cookies.get("session", "")
            cookie_authenticated = True
        if not token:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        try:
            claims = jwt.decode(
                token,
                settings.oidc_public_key,
                algorithms=["RS256"],
                audience=settings.oidc_audience,
                issuer=settings.oidc_issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as error:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from error

        subject = claims["sub"]
        if not isinstance(subject, str) or not subject.strip() or len(subject) > 255:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        organisation_id = request.headers.get("X-Organisation-ID")
        if identity_lookup is None and organisation_id is not None:
            try:
                organisation_id = str(UUID(organisation_id))
            except ValueError as error:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED) from error
        try:
            if identity_lookup is None:
                scope = await run_in_threadpool(resolve_identity_from_db, subject, organisation_id)
            else:
                scope = await run_in_threadpool(identity_lookup, subject)
                if scope is not None and organisation_id is not None and scope.organisation_id != organisation_id:
                    scope = None
        except IdentityStoreError as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from error
        except Exception as error:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE) from error
        if scope is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)

        if cookie_authenticated and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("Origin")
            supplied = request.headers.get("X-CSRF-Token", "")
            expected = csrf_token_for_session(token, settings.csrf_secret)
            if origin not in settings.allowed_origins or not hmac.compare_digest(supplied, expected):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="CSRF validation failed")

        request.scope.setdefault("state", {})["auth_scope"] = scope
        return scope

    return get_scope
