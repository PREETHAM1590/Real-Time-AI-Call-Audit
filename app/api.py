from collections.abc import Mapping

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

from app.auth import IdentityLookup, Scope, can_access, scope_dependency
from app.config import Settings

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
        allow_methods=["GET"],
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

    return app
