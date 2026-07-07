"""Auth tests: hashing, bootstrap, sessions, bearer tokens, scopes, auth mode none.

Uses an in-test FakeDb (dicts) conforming to the Database contract — the real Database is
exercised by the db module's own tests.
"""

from __future__ import annotations

import dataclasses
import types
from typing import Annotated, cast

import httpx
import pytest
from fastapi import Depends, FastAPI

from vidette.api.routers.auth import router as auth_router
from vidette.auth.deps import SESSION_COOKIE, current_principal, require_scope
from vidette.auth.service import (
    SESSION_TTL_S,
    AuthError,
    AuthService,
    Principal,
    hash_password,
    hash_token,
    new_api_token,
    new_session_token,
    verify_password,
)
from vidette.core.config import AuthMode, VidetteConfig
from vidette.db import ApiTokenRow, Database, SessionRow, UserRow

# --- in-test fakes ----------------------------------------------------------------------------


class FakeDb:
    """Dict-backed stand-in implementing the Database methods the auth service uses."""

    def __init__(self) -> None:
        self.users: dict[int, UserRow] = {}
        self.sessions: dict[str, SessionRow] = {}
        self.tokens: dict[int, ApiTokenRow] = {}
        self._next_user_id = 1
        self._next_token_id = 1

    async def count_users(self) -> int:
        return len(self.users)

    async def create_user(self, username: str, password_hash: str, role: str = "admin") -> int:
        if any(user.username == username for user in self.users.values()):
            raise ValueError(f"username {username!r} already exists")
        user_id = self._next_user_id
        self._next_user_id += 1
        self.users[user_id] = UserRow(
            id=user_id, username=username, password_hash=password_hash, role=role, created_at=0.0
        )
        return user_id

    async def get_user_by_username(self, username: str) -> UserRow | None:
        return next((u for u in self.users.values() if u.username == username), None)

    async def get_user(self, user_id: int) -> UserRow | None:
        return self.users.get(user_id)

    async def create_session(self, token_hash: str, user_id: int, expires_at: float) -> None:
        self.sessions[token_hash] = SessionRow(
            token_hash=token_hash, user_id=user_id, created_at=0.0, expires_at=expires_at
        )

    async def get_session(self, token_hash: str) -> SessionRow | None:
        return self.sessions.get(token_hash)

    async def delete_session(self, token_hash: str) -> None:
        self.sessions.pop(token_hash, None)

    async def create_api_token(
        self, name: str, token_hash: str, scopes: str, user_id: int
    ) -> int:
        token_id = self._next_token_id
        self._next_token_id += 1
        self.tokens[token_id] = ApiTokenRow(
            id=token_id,
            name=name,
            token_hash=token_hash,
            scopes=scopes,
            user_id=user_id,
            created_at=0.0,
            last_used_at=None,
            revoked_at=None,
        )
        return token_id

    async def get_api_token_by_hash(self, token_hash: str) -> ApiTokenRow | None:
        return next((t for t in self.tokens.values() if t.token_hash == token_hash), None)

    async def list_api_tokens(self) -> list[ApiTokenRow]:
        return list(self.tokens.values())

    async def revoke_api_token(self, token_id: int, now: float) -> bool:
        row = self.tokens.get(token_id)
        if row is None or row.revoked_at is not None:
            return False
        self.tokens[token_id] = dataclasses.replace(row, revoked_at=now)
        return True

    async def touch_api_token(self, token_id: int, now: float) -> None:
        row = self.tokens.get(token_id)
        if row is not None:
            self.tokens[token_id] = dataclasses.replace(row, last_used_at=now)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


def make_service(
    db: FakeDb,
    mode: AuthMode = AuthMode.builtin,
    *,
    clock: FakeClock | None = None,
    sleep: SleepRecorder | None = None,
) -> AuthService:
    return AuthService(
        cast(Database, db),
        mode,
        clock=clock or FakeClock(),
        sleep=sleep or SleepRecorder(),
    )


def make_app(config: VidetteConfig, service: AuthService) -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    app.state.runtime = types.SimpleNamespace(auth=service, config=config)
    return app


def make_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


UNAUTHORIZED_DETAIL = {
    "type": "about:blank",
    "title": "Unauthorized",
    "detail": "Log in at /, or send Authorization: Bearer <token>",
}


# --- password hashing ---------------------------------------------------------------------------


def test_password_hash_roundtrip() -> None:
    stored = hash_password("correct horse battery staple")
    assert stored.startswith("scrypt$16384$8$1$")
    assert verify_password("correct horse battery staple", stored)
    assert not verify_password("wrong password entirely", stored)
    # random salt: hashing twice never yields the same string
    assert hash_password("correct horse battery staple") != stored


def test_verify_password_tampered_stored_strings() -> None:
    stored = hash_password("a fine password")
    prefix, _, key_b64 = stored.rpartition("$")
    flipped = ("A" if key_b64[0] != "A" else "B") + key_b64[1:]
    tampered = [
        "",
        "plaintext",
        "scrypt$16384$8$1$only-five-fields",
        "bcrypt$16384$8$1$AAAA$BBBB",  # wrong algorithm tag
        "scrypt$notanint$8$1$AAAA$BBBB",
        "scrypt$16384$8$1$!!notb64!!$AAAA",
        "scrypt$16383$8$1$" + stored.split("$", 4)[4],  # n not a power of two
        "scrypt$16384$8$1$$",  # empty salt and key
        prefix + "$" + flipped,  # valid base64, wrong key bytes
    ]
    for bad in tampered:
        assert not verify_password("a fine password", bad), bad


def test_token_helpers() -> None:
    assert new_api_token().startswith("vd_")
    assert not new_session_token().startswith("vd_")
    digest = hash_token("vd_example")
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)
    assert digest != hash_token("vd_other")


# --- bootstrap ----------------------------------------------------------------------------------


async def test_bootstrap_once(test_config: VidetteConfig) -> None:
    app = make_app(test_config, make_service(FakeDb()))
    async with make_client(app) as client:
        status = (await client.get("/api/v1/auth/status")).json()
        assert status == {"bootstrapped": False, "mode": "builtin"}

        resp = await client.post(
            "/api/v1/auth/bootstrap", json={"username": "admin", "password": "longenough1"}
        )
        assert resp.status_code == 200
        assert resp.json() == {"username": "admin", "role": "admin"}
        assert SESSION_COOKIE in resp.cookies

        status = (await client.get("/api/v1/auth/status")).json()
        assert status["bootstrapped"] is True

        again = await client.post(
            "/api/v1/auth/bootstrap", json={"username": "other", "password": "longenough1"}
        )
        assert again.status_code == 409


async def test_bootstrap_validation(test_config: VidetteConfig) -> None:
    app = make_app(test_config, make_service(FakeDb()))
    async with make_client(app) as client:
        short = await client.post(
            "/api/v1/auth/bootstrap", json={"username": "admin", "password": "short"}
        )
        assert short.status_code == 400
        assert "10" in short.json()["detail"]["detail"]

        bad_name = await client.post(
            "/api/v1/auth/bootstrap", json={"username": "AB", "password": "longenough1"}
        )
        assert bad_name.status_code == 400


# --- login / sessions -----------------------------------------------------------------------


async def test_login_sets_cookie_and_me_works(test_config: VidetteConfig) -> None:
    db = FakeDb()
    app = make_app(test_config, make_service(db))
    async with make_client(app) as setup:
        await setup.post(
            "/api/v1/auth/bootstrap", json={"username": "admin", "password": "longenough1"}
        )
    async with make_client(app) as client:
        me_before = await client.get("/api/v1/auth/me")
        assert me_before.status_code == 401
        assert me_before.json()["detail"] == UNAUTHORIZED_DETAIL

        resp = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "longenough1"}
        )
        assert resp.status_code == 200
        set_cookie = resp.headers["set-cookie"]
        assert SESSION_COOKIE in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert f"Max-Age={SESSION_TTL_S}" in set_cookie
        assert "Path=/" in set_cookie

        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json() == {
            "username": "admin",
            "role": "admin",
            "via": "session",
            "scopes": ["admin"],
        }

        sessions_before_logout = len(db.sessions)
        logout = await client.post("/api/v1/auth/logout")
        assert logout.status_code == 204
        assert len(db.sessions) == sessions_before_logout - 1  # this session is gone
        assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_bad_login_uniform_401(test_config: VidetteConfig) -> None:
    app = make_app(test_config, make_service(FakeDb()))
    async with make_client(app) as client:
        await client.post(
            "/api/v1/auth/bootstrap", json={"username": "admin", "password": "longenough1"}
        )
        wrong_password = await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "not-the-password"}
        )
        unknown_user = await client.post(
            "/api/v1/auth/login", json={"username": "ghost", "password": "not-the-password"}
        )
        assert wrong_password.status_code == unknown_user.status_code == 401
        # uniform: no username oracle in status or body
        assert wrong_password.json() == unknown_user.json()


async def test_login_backoff_grows_capped_and_resets() -> None:
    db = FakeDb()
    sleep = SleepRecorder()
    service = make_service(db, sleep=sleep)
    await service.bootstrap("admin", "longenough1")

    for _ in range(7):
        with pytest.raises(AuthError):
            await service.login("admin", "wrong")
    # first failure has no prior strikes → no delay; later ones grow and cap at 5 s
    assert len(sleep.calls) == 6
    assert sleep.calls == sorted(sleep.calls)
    assert sleep.calls[-1] == 5.0
    assert all(delay <= 5.0 for delay in sleep.calls)

    await service.login("admin", "longenough1")  # success resets the counter
    sleep.calls.clear()
    with pytest.raises(AuthError):
        await service.login("admin", "wrong")
    assert sleep.calls == []  # no strikes on record → no delay


async def test_expired_session_deleted_on_sight() -> None:
    db = FakeDb()
    clock = FakeClock()
    service = make_service(db, clock=clock)
    await service.bootstrap("admin", "longenough1")
    token, _ = await service.login("admin", "longenough1")
    assert await service.authenticate_session(token) is not None

    clock.now += SESSION_TTL_S + 1
    assert await service.authenticate_session(token) is None
    assert db.sessions == {}  # expired row was deleted, not just ignored


# --- bearer tokens --------------------------------------------------------------------------


async def test_bearer_token_create_use_revoke(test_config: VidetteConfig) -> None:
    app = make_app(test_config, make_service(FakeDb()))
    async with make_client(app) as client:
        await client.post(
            "/api/v1/auth/bootstrap", json={"username": "admin", "password": "longenough1"}
        )
        created = await client.post(
            "/api/v1/auth/tokens", json={"name": "ci", "scopes": ["read:events"]}
        )
        assert created.status_code == 200
        token = created.json()["token"]
        token_id = created.json()["id"]
        assert token.startswith("vd_")

        listed = await client.get("/api/v1/auth/tokens")
        assert listed.status_code == 200
        (info,) = listed.json()
        assert info["id"] == token_id
        assert info["name"] == "ci"
        assert info["scopes"] == ["read:events"]
        assert "token_hash" not in info  # hashes never leave the server
        assert token not in listed.text

    async with make_client(app) as bearer:  # fresh client: no cookie jar
        headers = {"Authorization": f"Bearer {token}"}
        me = await bearer.get("/api/v1/auth/me", headers=headers)
        assert me.status_code == 200
        assert me.json() == {
            "username": "admin",
            "role": "admin",
            "via": "token",
            "scopes": ["read:events"],
        }
        # token scopes are enforced: read:events does not grant admin
        assert (await bearer.get("/api/v1/auth/tokens", headers=headers)).status_code == 403

    async with make_client(app) as client:
        await client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "longenough1"}
        )
        assert (await client.delete(f"/api/v1/auth/tokens/{token_id}")).status_code == 204
        assert (await client.delete(f"/api/v1/auth/tokens/{token_id}")).status_code == 404

    async with make_client(app) as bearer:
        revoked = await bearer.get(
            "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
        )
        assert revoked.status_code == 401
        assert revoked.json()["detail"] == UNAUTHORIZED_DETAIL


async def test_create_token_rejects_unknown_scope(test_config: VidetteConfig) -> None:
    app = make_app(test_config, make_service(FakeDb()))
    async with make_client(app) as client:
        await client.post(
            "/api/v1/auth/bootstrap", json={"username": "admin", "password": "longenough1"}
        )
        resp = await client.post(
            "/api/v1/auth/tokens", json={"name": "bad", "scopes": ["launch:missiles"]}
        )
        assert resp.status_code == 400
        assert "read:events" in resp.json()["detail"]["detail"]  # lists valid scopes


# --- scope enforcement ------------------------------------------------------------------------


def _add_protected_route(app: FastAPI) -> None:
    @app.get("/protected")
    async def protected(
        principal: Annotated[Principal, Depends(require_scope("write:config"))],
    ) -> dict[str, str]:
        return {"username": principal.username}


async def test_require_scope_enforcement(test_config: VidetteConfig) -> None:
    app = make_app(test_config, make_service(FakeDb()))
    _add_protected_route(app)

    viewer = Principal(
        user_id=7,
        username="watcher",
        role="viewer",
        scopes=frozenset({"read:events", "read:streams", "read:config"}),
        via="session",
    )
    admin = Principal(
        user_id=1, username="admin", role="admin", scopes=frozenset({"admin"}), via="session"
    )

    async with make_client(app) as client:
        unauthenticated = await client.get("/protected")
        assert unauthenticated.status_code == 401

        app.dependency_overrides[current_principal] = lambda: viewer
        forbidden = await client.get("/protected")
        assert forbidden.status_code == 403
        assert "write:config" in forbidden.json()["detail"]["detail"]

        app.dependency_overrides[current_principal] = lambda: admin  # admin implies all
        allowed = await client.get("/protected")
        assert allowed.status_code == 200
        assert allowed.json() == {"username": "admin"}


# --- auth mode none -------------------------------------------------------------------------


async def test_mode_none_yields_anonymous_admin(test_config: VidetteConfig) -> None:
    config = test_config.model_copy(deep=True)
    config.server.auth.mode = AuthMode.none
    app = make_app(config, make_service(FakeDb(), AuthMode.none))
    _add_protected_route(app)

    async with make_client(app) as client:
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json() == {
            "username": "anonymous",
            "role": "admin",
            "via": "anonymous",
            "scopes": ["admin"],
        }
        assert (await client.get("/protected")).status_code == 200
        status = (await client.get("/api/v1/auth/status")).json()
        assert status["mode"] == "none"
