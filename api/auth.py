"""API authentication for a public demo: named demo users, signed session cookies, rate limits.

Users come from ``DEMO_USERS``: comma-separated ``name:scrypt$<salt_hex>$<hash_hex>`` or
``name:role:scrypt$...`` with role ``requester`` (may create runs), ``approver`` (may decide
approvals) or ``both`` (default). Hashes are stdlib ``hashlib.scrypt`` (n=2**14, r=8, p=1,
32-byte key, 16-byte salt), compared in constant time; ``scripts/hash_password.py`` makes them.

A login sets one cookie, ``HttpOnly``, ``SameSite=Strict``, ``Secure`` when ``COOKIE_SECURE=1``:
``<payload_b64>.<hmac_b64>`` where the payload is ``{"u": user, "r": role, "exp": ..., "jti":
...}`` and the MAC is HMAC-SHA256 with ``SESSION_SECRET`` over a domain-separated string. It is
valid for ``SESSION_TTL_S`` (8 h) and only while the user is still listed with the same role
(removing a user from ``DEMO_USERS`` and restarting revokes their sessions). Logout adds the
token id to an in-process deny list until it expires.

Fail closed: ``AuthConfig.from_env`` raises ``AuthConfigError`` when ``DEMO_USERS`` or
``SESSION_SECRET`` is missing or malformed, so the API refuses to start, unless
``API_AUTH_DISABLED=1`` is set explicitly (development and tests; a loud warning is logged).

Rate limits are in-memory token buckets (``RateLimiter``), so they hold per API process: run
several workers or replicas and each has its own buckets (put a shared limiter at the proxy
for that).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["requester", "approver", "both"]
ROLES: tuple[str, ...] = ("requester", "approver", "both")

USERS_ENV = "DEMO_USERS"
SECRET_ENV = "SESSION_SECRET"
DISABLED_ENV = "API_AUTH_DISABLED"
COOKIE_SECURE_ENV = "COOKIE_SECURE"
SEPARATE_APPROVER_ENV = "REQUIRE_SEPARATE_APPROVER"

COOKIE_NAME = "ral_session"
SESSION_TTL_S = 8 * 60 * 60
MIN_SECRET_CHARS = 32

SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SCRYPT_DKLEN = 32
SALT_BYTES = 16
_SCHEME = "scrypt"
_SESSION_DOMAIN = "session-v1:"
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class AuthConfigError(RuntimeError):
    """Authentication is not configured: the API must not start."""


# --- passwords --------------------------------------------------------------------------------


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=SCRYPT_DKLEN,
        maxmem=64 * 1024 * 1024,
    )


def hash_password(password: str, salt: bytes | None = None) -> str:
    """``scrypt$<salt_hex>$<hash_hex>`` for ``DEMO_USERS``."""
    if not password:
        raise ValueError("empty password")
    salt = salt if salt is not None else secrets.token_bytes(SALT_BYTES)
    return f"{_SCHEME}${salt.hex()}${_scrypt(password, salt).hex()}"


def _parse_hash(encoded: str) -> tuple[bytes, bytes]:
    scheme, _, rest = encoded.partition("$")
    salt_hex, _, hash_hex = rest.partition("$")
    if scheme != _SCHEME or not salt_hex or not hash_hex:
        raise ValueError("password hash must be scrypt$<salt_hex>$<hash_hex>")
    try:
        salt, digest = bytes.fromhex(salt_hex), bytes.fromhex(hash_hex)
    except ValueError as exc:
        raise ValueError("password hash is not hex") from exc
    if len(salt) < 8 or len(digest) != SCRYPT_DKLEN:
        raise ValueError("password hash has the wrong length")
    return salt, digest


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check of ``password`` against a ``scrypt$...`` value."""
    salt, digest = _parse_hash(encoded)
    return hmac.compare_digest(_scrypt(password, salt), digest)


# Compared against for unknown names, so a login takes as long whether the user exists or not.
_DUMMY_HASH = hash_password("not-a-user", salt=b"\0" * SALT_BYTES)


# --- users ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class User:
    name: str
    role: Role = "both"

    @property
    def can_request(self) -> bool:
        return self.role in ("requester", "both")

    @property
    def can_approve(self) -> bool:
        return self.role in ("approver", "both")


def parse_users(raw: str) -> dict[str, tuple[User, str]]:
    """``DEMO_USERS`` -> {name: (User, encoded hash)}. Raises ``ValueError`` on any bad entry."""
    users: dict[str, tuple[User, str]] = {}
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) == 2:
            name, role, encoded = parts[0], "both", parts[1]
        elif len(parts) == 3:
            name, role, encoded = parts
        else:
            raise ValueError("each DEMO_USERS entry is name:hash or name:role:hash")
        if not _NAME_RE.fullmatch(name):
            raise ValueError("user names are 1-64 of A-Z a-z 0-9 _ . -")
        if role not in ROLES:
            raise ValueError(f"role must be one of {', '.join(ROLES)}")
        if name in users:
            raise ValueError("duplicate user name")
        _parse_hash(encoded)
        users[name] = (User(name=name, role=role), encoded)  # type: ignore[arg-type]
    return users


# --- sessions ---------------------------------------------------------------------------------


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass
class AuthConfig:
    enabled: bool
    users: dict[str, tuple[User, str]] = field(default_factory=dict)
    secret: bytes = b""
    cookie_secure: bool = False
    require_separate_approver: bool = False
    session_ttl_s: int = SESSION_TTL_S
    # Requests per minute: logins per client IP, run creations/approvals per user.
    login_per_min: int = 5
    runs_per_min: int = 20
    clock: Callable[[], float] = time.time
    _revoked: dict[str, int] = field(default_factory=dict)
    _revoked_lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AuthConfig:
        env = dict(os.environ) if env is None else env
        separate = env.get(SEPARATE_APPROVER_ENV) == "1"
        if env.get(DISABLED_ENV) == "1":
            return cls(enabled=False, require_separate_approver=separate)
        raw_users = (env.get(USERS_ENV) or "").strip()
        secret = (env.get(SECRET_ENV) or "").strip()
        if not raw_users or not secret:
            raise AuthConfigError(
                f"{USERS_ENV} and {SECRET_ENV} must be set (or {DISABLED_ENV}=1 for local "
                "development only); see scripts/hash_password.py"
            )
        if len(secret) < MIN_SECRET_CHARS:
            raise AuthConfigError(f"{SECRET_ENV} must be at least {MIN_SECRET_CHARS} characters")
        try:
            users = parse_users(raw_users)
        except ValueError as exc:
            raise AuthConfigError(f"{USERS_ENV}: {exc}") from exc
        if not users:
            raise AuthConfigError(f"{USERS_ENV} lists no user")
        return cls(
            enabled=True,
            users=users,
            secret=secret.encode("utf-8"),
            cookie_secure=env.get(COOKIE_SECURE_ENV) == "1",
            require_separate_approver=separate,
        )

    # Passwords -------------------------------------------------------------------------------

    def authenticate(self, name: str, password: str) -> User | None:
        entry = self.users.get(name)
        ok = verify_password(password, entry[1] if entry else _DUMMY_HASH)
        return entry[0] if entry and ok else None

    # Session tokens --------------------------------------------------------------------------

    def _mac(self, payload: str) -> bytes:
        return hmac.new(self.secret, (_SESSION_DOMAIN + payload).encode(), hashlib.sha256).digest()

    def issue(self, user: User) -> str:
        claims = {
            "u": user.name,
            "r": user.role,
            "exp": int(self.clock()) + self.session_ttl_s,
            "jti": secrets.token_hex(12),
        }
        payload = json.dumps(claims, sort_keys=True, separators=(",", ":"))
        return f"{_b64e(payload.encode())}.{_b64e(self._mac(payload))}"

    def _claims(self, token: str | None) -> dict | None:
        if not token or len(token) > 1024:
            return None
        try:
            payload_b64, sig_b64 = token.split(".")
            payload = _b64d(payload_b64).decode("utf-8")
            sig = _b64d(sig_b64)
            claims = json.loads(payload)
            exp = int(claims["exp"])
        except (ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
            return None
        if not hmac.compare_digest(sig, self._mac(payload)):
            return None
        if self.clock() >= exp:
            return None
        with self._revoked_lock:
            if claims.get("jti") in self._revoked:
                return None
        return claims

    def user_for(self, token: str | None) -> User | None:
        """The session's user, or None for a missing, forged, expired or revoked session."""
        claims = self._claims(token)
        if claims is None:
            return None
        entry = self.users.get(claims.get("u", ""))
        # The user must still exist with the same role: a role change ends old sessions.
        if entry is None or entry[0].role != claims.get("r"):
            return None
        return entry[0]

    def revoke(self, token: str | None) -> None:
        claims = self._claims(token)
        if claims is None:
            return
        now = int(self.clock())
        with self._revoked_lock:
            self._revoked = {j: e for j, e in self._revoked.items() if e > now}
            self._revoked[str(claims.get("jti"))] = int(claims["exp"])


# --- rate limiting ----------------------------------------------------------------------------


class RateLimiter:
    """Token buckets in memory: ``capacity`` requests, refilled evenly over ``per_s`` seconds.

    Per process only (see the module docstring). Bounded: idle full buckets are dropped.
    """

    MAX_KEYS = 10_000

    def __init__(
        self, capacity: int, per_s: float = 60.0, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self.capacity = float(capacity)
        self.rate = capacity / per_s
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    def take(self, key: str) -> float | None:
        """None when allowed; else the seconds to wait before the next request is allowed."""
        now = self._clock()
        with self._lock:
            tokens, last = self._buckets.get(key, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed = None
            else:
                self._buckets[key] = (tokens, now)
                allowed = (1.0 - tokens) / self.rate
            if len(self._buckets) > self.MAX_KEYS:
                self._prune(now)
        return allowed

    def _prune(self, now: float) -> None:
        full = [
            k
            for k, (t, last) in self._buckets.items()
            if t + (now - last) * self.rate >= self.capacity
        ]
        for k in full:
            del self._buckets[k]


def retry_after_header(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


__all__ = [
    "COOKIE_NAME",
    "ROLES",
    "SESSION_TTL_S",
    "AuthConfig",
    "AuthConfigError",
    "RateLimiter",
    "User",
    "hash_password",
    "parse_users",
    "retry_after_header",
    "verify_password",
]
