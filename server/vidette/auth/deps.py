"""FastAPI auth dependencies.

Wiring assumptions (kept stable for routers):
- `request.app.state.runtime` is the AppRuntime (vidette/runtime.py) exposing `.auth`
  and `.config`.
- Session cookie name is SESSION_COOKIE; bearer tokens come via `Authorization: Bearer vd_…`.
- With `auth.mode == none`, `current_principal` returns ANONYMOUS_ADMIN (the /system
  endpoint separately reports the loud warning).
- Failures raise HTTPException(401) with a problem-json-shaped detail dict:
  {"type": "about:blank", "title": "Unauthorized", "detail": "<what to do next>"}.
- `require_scope("read:streams")` returns an async dependency (itself depending on
  `current_principal`, so tests can override just that) which 403s when the principal
  lacks the scope.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from vidette.auth.service import ANONYMOUS_ADMIN, AuthService, Principal
from vidette.core.config import AuthMode

SESSION_COOKIE = "vidette_session"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={
            "type": "about:blank",
            "title": "Unauthorized",
            "detail": "Log in at /, or send Authorization: Bearer <token>",
        },
    )


async def current_principal(request: Request) -> Principal:
    runtime = request.app.state.runtime
    if runtime.config.server.auth.mode is AuthMode.none:
        return ANONYMOUS_ADMIN
    auth: AuthService = runtime.auth
    header = request.headers.get("Authorization")
    if header is not None:
        scheme, _, credentials = header.partition(" ")
        token = credentials.strip()
        if scheme.lower() == "bearer" and token:
            principal = await auth.authenticate_bearer(token)
            if principal is not None:
                return principal
            raise _unauthorized()  # an explicit bearer attempt never falls back to cookies
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token:
        session_principal = await auth.authenticate_session(session_token)
        if session_principal is not None:
            return session_principal
    raise _unauthorized()


def require_scope(scope: str) -> Callable[..., Coroutine[Any, Any, Principal]]:
    """Dependency factory: 403 unless the authenticated principal allows `scope`."""

    async def dependency(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> Principal:
        if not principal.allows(scope):
            raise HTTPException(
                status_code=403,
                detail={
                    "type": "about:blank",
                    "title": "Forbidden",
                    "detail": f"this action requires the '{scope}' scope — "
                    "ask an admin, or use a token that carries it",
                },
            )
        return principal

    return dependency
