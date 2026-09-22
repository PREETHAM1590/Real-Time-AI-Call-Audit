from collections.abc import Callable
from dataclasses import dataclass

import jwt
from fastapi import HTTPException, Request, status

from app.config import Settings


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


IdentityLookup = Callable[[str], Scope | None]


def scope_dependency(settings: Settings, identity_lookup: IdentityLookup):
    """Validate an OIDC JWT, then resolve server-owned scope for its subject."""

    async def get_scope(request: Request) -> Scope:
        authorization = request.headers.get("Authorization", "")
        token = (
            authorization.removeprefix("Bearer ")
            if authorization.startswith("Bearer ")
            else request.cookies.get("session")
        )
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
        if not isinstance(subject, str):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        scope = identity_lookup(subject)
        if scope is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return scope

    return get_scope
