"""Security primitives: Argon2id password hashing, access-JWT encode/decode,
and opaque refresh-token helpers (§7.1).

This module is crypto only — the authenticated-user dependency and the RBAC
``require()`` guard live in ``app.core.permissions``.
"""

import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
from pwdlib import PasswordHash

from app.core.config import Settings
from app.core.exceptions import UnauthorizedError

JWT_ALGORITHM = "HS256"

_password_hasher = PasswordHash.recommended()

# Argon2id cost parameters, as a *deployment* constraint rather than a tuning
# knob (§16). ``PasswordHash.recommended()`` resolves to m=64MiB, t=3, p=4 —
# the OWASP-recommended floor, and deliberately left there: lowering the memory
# cost is exactly what makes an offline crack of a stolen hash cheaper, which is
# the attack this parameter exists to price up.
#
# The consequence is that **every concurrent password hash holds 64 MiB**, so a
# container's memory limit has to cover ``ARGON2_MEMORY_MIB * concurrent
# hashes`` on top of the app's own footprint, or the kernel OOM-kills the
# process under an authentication burst. Hashing is CPU-bound and runs inline in
# the event loop, so concurrent hashes per container is bounded by the process
# count (``WEB_CONCURRENCY``), not by request concurrency — one uvicorn worker
# hashes one password at a time.
#
# Budget for a container: WEB_CONCURRENCY * 64 MiB + ~256 MiB baseline.
# The §16 default (WEB_CONCURRENCY=2) needs ~384 MiB; the audit's 512 MiB pod
# fits it, but raising WEB_CONCURRENCY without raising the memory limit is the
# way to break it. ``scripts/check_argon2_budget.py`` checks a given limit.
ARGON2_MEMORY_MIB = 64
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 4

# Verified against when a login hits an unknown email, so the response takes
# as long as a real Argon2 check — no user enumeration via timing (§7.1).
DUMMY_PASSWORD_HASH = _password_hasher.hash("dummy-password-for-timing")


def hash_password(password: str) -> str:
    return _password_hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return _password_hasher.verify(password, password_hash)


def generate_refresh_token() -> str:
    """Opaque refresh token; only its SHA-256 is ever stored (§7.1)."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 hex digest for refresh/reset/verification tokens — a stolen DB
    or Redis snapshot must not contain usable tokens."""
    return hashlib.sha256(token.encode()).hexdigest()


def sign_value(purpose: str, value: str, settings: Settings) -> str:
    """Stateless signed token ``value.hmac`` — for links that must survive
    longer than a Redis TTL (e.g. per-saved-search unsubscribe, §8.9/§10.12).
    ``purpose`` domain-separates signatures so a token minted for one link
    type can never be replayed as another."""
    sig = hmac.new(
        settings.app_secret_key.encode(), f"{purpose}:{value}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{value}.{sig}"


def unsign_value(purpose: str, token: str, settings: Settings) -> str | None:
    """The verified value, or ``None`` for a missing/forged signature."""
    value, sep, sig = token.rpartition(".")
    if not sep:
        return None
    expected = hmac.new(
        settings.app_secret_key.encode(), f"{purpose}:{value}".encode(), hashlib.sha256
    ).hexdigest()
    return value if hmac.compare_digest(sig, expected) else None


def jti_denylist_key(jti: str) -> str:
    return f"auth:jti:deny:{jti}"


def user_jtis_key(user_id: uuid.UUID) -> str:
    """Set of a user's live access-token jtis — lets disable/logout-all/reset
    denylist every outstanding token at once instead of waiting out the TTL."""
    return f"auth:jti:all:{user_id}"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Validated claims of an access JWT. ``tenant_id`` is ``None`` for
    platform-staff tokens (§7.2). ``impersonator_id`` is set only on an
    impersonation token (§8.16/§10.11) — the platform staff acting as the
    tenant user — and is the frontend's "impersonation active" signal."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    role: str
    jti: str
    impersonator_id: uuid.UUID | None = None


def create_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    settings: Settings,
    ttl_seconds: int | None = None,
    impersonator_id: uuid.UUID | None = None,
) -> tuple[str, str]:
    """Returns ``(token, jti)``. The ``jti`` is what logout denylists.

    ``ttl_seconds`` overrides the default access-token lifetime (impersonation
    tokens are time-boxed short, §8.16). ``impersonator_id`` stamps an ``imp``
    claim so the token is a distinguishable, auditable impersonation session."""
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(seconds=ttl),
    }
    if tenant_id is not None:
        claims["tid"] = str(tenant_id)
    if impersonator_id is not None:
        claims["imp"] = str(impersonator_id)
    token = jwt.encode(claims, settings.app_secret_key, algorithm=JWT_ALGORITHM)
    return token, jti


def decode_access_token(token: str, settings: Settings) -> TokenClaims:
    try:
        claims = jwt.decode(
            token,
            settings.app_secret_key,
            algorithms=[JWT_ALGORITHM],
            options={"require": ["sub", "role", "jti", "exp", "iat"]},
        )
        tid = claims.get("tid")
        imp = claims.get("imp")
        return TokenClaims(
            user_id=uuid.UUID(claims["sub"]),
            tenant_id=uuid.UUID(tid) if tid is not None else None,
            role=claims["role"],
            jti=claims["jti"],
            impersonator_id=uuid.UUID(imp) if imp is not None else None,
        )
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise UnauthorizedError("The access token is invalid or has expired.") from exc
