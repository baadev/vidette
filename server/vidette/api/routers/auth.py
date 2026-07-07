"""Auth endpoints: first-run bootstrap, sessions, /me, scoped API tokens.

Security posture (docs/architecture/security-model.md): no default credentials — the first
request to POST /bootstrap creates the only admin; login failures are uniform (no username
oracle); API tokens are shown once and only their hashes are stored; token management is
admin-only. The session cookie is httpOnly + SameSite=Lax, path=/.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from vidette.auth.deps import SESSION_COOKIE, current_principal, require_scope
from vidette.auth.service import SESSION_TTL_S, AuthError, AuthService, Principal

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class StatusResponse(BaseModel):
    bootstrapped: bool
    mode: str


class Credentials(BaseModel):
    username: str
    password: str


class UserResponse(BaseModel):
    username: str
    role: str


class MeResponse(BaseModel):
    username: str
    role: str
    via: str
    scopes: list[str]


class TokenCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scopes: list[str] = Field(min_length=1)


class TokenCreateResponse(BaseModel):
    token: str
    id: int


class TokenInfo(BaseModel):
    id: int
    name: str
    scopes: list[str]
    user_id: int
    created_at: float
    last_used_at: float | None
    revoked_at: float | None


def _auth(request: Request) -> AuthService:
    service: AuthService = request.app.state.runtime.auth
    return service


def _problem(status_code: int, title: str, detail: str) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"type": "about:blank", "title": title, "detail": detail},
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=SESSION_TTL_S,
        path="/",
    )


@router.get("/status")
async def status(request: Request) -> StatusResponse:
    """Public: lets the first-run wizard decide whether to show bootstrap or login."""
    runtime = request.app.state.runtime
    return StatusResponse(
        bootstrapped=await _auth(request).bootstrapped(),
        mode=runtime.config.server.auth.mode.value,
    )


@router.post("/bootstrap")
async def bootstrap(request: Request, body: Credentials, response: Response) -> UserResponse:
    auth = _auth(request)
    if await auth.bootstrapped():
        raise _problem(
            409, "Conflict", "already bootstrapped — log in at /api/v1/auth/login instead"
        )
    try:
        principal = await auth.bootstrap(body.username, body.password)
        token, _ = await auth.login(body.username, body.password)
    except AuthError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    _set_session_cookie(response, token)
    return UserResponse(username=principal.username, role=principal.role)


@router.post("/login")
async def login(request: Request, body: Credentials, response: Response) -> UserResponse:
    try:
        token, principal = await _auth(request).login(body.username, body.password)
    except AuthError as exc:
        # Uniform: same status and body whether the username or the password was wrong.
        raise _problem(401, "Unauthorized", str(exc)) from exc
    _set_session_cookie(response, token)
    return UserResponse(username=principal.username, role=principal.role)


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response) -> None:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await _auth(request).logout(token)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me")
async def me(principal: Annotated[Principal, Depends(current_principal)]) -> MeResponse:
    return MeResponse(
        username=principal.username,
        role=principal.role,
        via=principal.via,
        scopes=sorted(principal.scopes),
    )


@router.post("/tokens")
async def create_token(
    request: Request,
    body: TokenCreateRequest,
    principal: Annotated[Principal, Depends(require_scope("admin"))],
) -> TokenCreateResponse:
    try:
        token, token_id = await _auth(request).create_api_token(
            body.name, frozenset(body.scopes), principal.user_id
        )
    except AuthError as exc:
        raise _problem(400, "Bad Request", str(exc)) from exc
    # Shown exactly once; only the sha256 hash is stored.
    return TokenCreateResponse(token=token, id=token_id)


@router.get("/tokens")
async def list_tokens(
    request: Request,
    principal: Annotated[Principal, Depends(require_scope("admin"))],
) -> list[TokenInfo]:
    rows = await _auth(request).list_api_tokens()
    return [
        TokenInfo(
            id=row.id,
            name=row.name,
            scopes=[scope for scope in row.scopes.split(",") if scope],
            user_id=row.user_id,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            revoked_at=row.revoked_at,
        )
        for row in rows
    ]


@router.delete("/tokens/{token_id}", status_code=204)
async def revoke_token(
    request: Request,
    token_id: int,
    principal: Annotated[Principal, Depends(require_scope("admin"))],
) -> None:
    if not await _auth(request).revoke_api_token(token_id):
        raise _problem(
            404, "Not Found", f"no API token with id {token_id} — list them at GET /tokens"
        )
