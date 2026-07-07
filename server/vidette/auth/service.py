"""Auth service: password hashing, sessions, scoped API tokens.

Implementation notes (binding, mirrored by tests):
- Password hashing: `hashlib.scrypt`, format "scrypt$<n>$<r>$<p>$<salt_b64>$<key_b64>",
  n=2**14, r=8, p=1, 32-byte key, 16-byte salt. `verify_password` parses parameters
  from the stored string (forward-compatible) and compares with `hmac.compare_digest`.
- Session tokens: `secrets.token_urlsafe(32)`; API tokens: "vd_" + token_urlsafe(32).
  Only sha256 hex digests of tokens are stored (`hash_token`).
- Login applies a small in-memory failure delay (exponential per username, capped at 5 s,
  reset on success). This is a brake, not a bouncer: it slows online guessing from a
  single process without locking anyone out; put a rate limiter in front for real abuse.
- SCOPES below is the closed set; "admin" implies all.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from vidette.core.config import AuthMode
from vidette.db import ApiTokenRow, Database, UserRow

SCOPES = frozenset({"read:events", "read:streams", "read:config", "write:config", "admin"})
SESSION_TTL_S = 14 * 24 * 3600
MIN_PASSWORD_LENGTH = 10

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_KEY_LEN = 32
_SCRYPT_SALT_LEN = 16

_USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{2,31}$")

# Scopes granted to interactive sessions per role. Tokens carry their own explicit scopes.
_ROLE_SCOPES: dict[str, frozenset[str]] = {
    "admin": frozenset({"admin"}),
    "viewer": frozenset({"read:events", "read:streams", "read:config"}),
}

# Verified (and discarded) when the username is unknown, so an attacker cannot tell
# "no such user" from "wrong password" by timing the scrypt work.
_DUMMY_HASH = (
    "scrypt$16384$8$1$Gxt+n6Ghym0C/IZHgnQCgw==$H2G3P5pSThEgsjlV2eVlLZcWLwNj5UmYM7GHUMBrtiQ="
)

_MAX_LOGIN_DELAY_S = 5.0


class AuthError(Exception):
    """Uniform, user-safe message (never reveals whether the username exists)."""


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SCRYPT_SALT_LEN)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_KEY_LEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    key_b64 = base64.b64encode(key).decode("ascii")
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${key_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time verify; a malformed/tampered stored string is simply a non-match."""
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4], validate=True)
        expected = base64.b64decode(parts[5], validate=True)
        if not salt or not expected:
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
        )
    except (ValueError, binascii.Error):  # bad ints, bad base64, invalid scrypt params
        return False
    return hmac.compare_digest(candidate, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def new_api_token() -> str:
    return "vd_" + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str  # "admin" | "viewer"
    scopes: frozenset[str]
    via: str  # "session" | "token" | "anonymous"

    def allows(self, scope: str) -> bool:
        return "admin" in self.scopes or scope in self.scopes


ANONYMOUS_ADMIN = Principal(
    user_id=0, username="anonymous", role="admin", scopes=frozenset({"admin"}), via="anonymous"
)


def _role_scopes(role: str) -> frozenset[str]:
    return _ROLE_SCOPES.get(role, frozenset())


class AuthService:
    def __init__(
        self,
        db: Database,
        mode: AuthMode,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.db = db
        self.mode = mode
        self._clock = clock
        self._sleep = sleep
        self._login_failures: dict[str, int] = {}

    async def bootstrapped(self) -> bool:
        return await self.db.count_users() > 0

    async def bootstrap(self, username: str, password: str) -> Principal:
        """Create the first admin. Raises AuthError if any user exists or password is weak."""
        if not _USERNAME_RE.match(username):
            raise AuthError(
                "username must be 3–32 characters of lowercase letters, digits, '-' or '_', "
                "starting with a letter or digit"
            )
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(
                f"password must be at least {MIN_PASSWORD_LENGTH} characters — "
                "pick a longer passphrase"
            )
        if await self.db.count_users() > 0:
            raise AuthError(
                "already bootstrapped — log in with the existing admin account instead"
            )
        try:
            user_id = await self.db.create_user(username, hash_password(password), role="admin")
        except ValueError as exc:  # duplicate username race
            raise AuthError(
                "already bootstrapped — log in with the existing admin account instead"
            ) from exc
        return Principal(
            user_id=user_id,
            username=username,
            role="admin",
            scopes=_role_scopes("admin"),
            via="session",
        )

    async def login(self, username: str, password: str) -> tuple[str, Principal]:
        """Returns (session_token, principal). Raises AuthError on any failure (uniform)."""
        failures = self._login_failures.get(username, 0)
        if failures > 0:
            await self._sleep(min(_MAX_LOGIN_DELAY_S, 0.25 * (2**failures)))
        user = await self.db.get_user_by_username(username)
        if user is None:
            # Burn comparable scrypt time so unknown usernames are not distinguishable
            # from wrong passwords by response timing.
            verify_password(password, _DUMMY_HASH)
            raise self._login_failed(username)
        if not verify_password(password, user.password_hash):
            raise self._login_failed(username)
        self._login_failures.pop(username, None)
        token = new_session_token()
        now = self._clock()
        await self.db.create_session(hash_token(token), user.id, now + SESSION_TTL_S)
        return token, self._principal_for(user, via="session")

    def _login_failed(self, username: str) -> AuthError:
        self._login_failures[username] = self._login_failures.get(username, 0) + 1
        return AuthError("invalid username or password — check the credentials and try again")

    async def logout(self, session_token: str) -> None:
        await self.db.delete_session(hash_token(session_token))

    async def authenticate_session(self, session_token: str) -> Principal | None:
        """None if missing/expired; expired sessions are deleted on sight."""
        row = await self.db.get_session(hash_token(session_token))
        if row is None:
            return None
        if row.expires_at <= self._clock():
            await self.db.delete_session(row.token_hash)
            return None
        user = await self.db.get_user(row.user_id)
        if user is None:  # user deleted; the session is dead weight
            await self.db.delete_session(row.token_hash)
            return None
        return self._principal_for(user, via="session")

    async def authenticate_bearer(self, token: str) -> Principal | None:
        """None if unknown/revoked; touches last_used_at on success."""
        row = await self.db.get_api_token_by_hash(hash_token(token))
        if row is None or row.revoked_at is not None:
            return None
        user = await self.db.get_user(row.user_id)
        if user is None:
            return None
        await self.db.touch_api_token(row.id, self._clock())
        scopes = frozenset(scope for scope in row.scopes.split(",") if scope)
        return Principal(
            user_id=user.id, username=user.username, role=user.role, scopes=scopes, via="token"
        )

    async def create_api_token(
        self, name: str, scopes: frozenset[str], user_id: int
    ) -> tuple[str, int]:
        """Returns (plaintext_token_shown_once, token_id). Validates scopes ⊆ SCOPES."""
        unknown = scopes - SCOPES
        if unknown:
            raise AuthError(
                f"unknown scope(s): {', '.join(sorted(unknown))} — "
                f"valid scopes are: {', '.join(sorted(SCOPES))}"
            )
        if not scopes:
            raise AuthError(
                f"a token needs at least one scope — valid scopes are: {', '.join(sorted(SCOPES))}"
            )
        token = new_api_token()
        token_id = await self.db.create_api_token(
            name, hash_token(token), ",".join(sorted(scopes)), user_id
        )
        return token, token_id

    async def list_api_tokens(self) -> list[ApiTokenRow]:
        """Token metadata for the settings UI; rows include hashes — never expose them."""
        return await self.db.list_api_tokens()

    async def revoke_api_token(self, token_id: int) -> bool:
        """True if the token existed and is now revoked."""
        return await self.db.revoke_api_token(token_id, self._clock())

    def _principal_for(self, user: UserRow, *, via: str) -> Principal:
        return Principal(
            user_id=user.id,
            username=user.username,
            role=user.role,
            scopes=_role_scopes(user.role),
            via=via,
        )
