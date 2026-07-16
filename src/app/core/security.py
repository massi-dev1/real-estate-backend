"""Security primitives: Argon2id password hashing, access-JWT encode/decode,
and opaque refresh-token helpers (§7.1).

This module is crypto only — the authenticated-user dependency and the RBAC
``require()`` guard live in ``app.core.permissions``.
"""

import hashlib
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


def jti_denylist_key(jti: str) -> str:
    return f"auth:jti:deny:{jti}"


def user_jtis_key(user_id: uuid.UUID) -> str:
    """Set of a user's live access-token jtis — lets disable/logout-all/reset
    denylist every outstanding token at once instead of waiting out the TTL."""
    return f"auth:jti:all:{user_id}"


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """Validated claims of an access JWT. ``tenant_id`` is ``None`` for
    platform-staff tokens (§7.2)."""

    user_id: uuid.UUID
    tenant_id: uuid.UUID | None
    role: str
    jti: str


def create_access_token(
    *,
    user_id: uuid.UUID,
    tenant_id: uuid.UUID | None,
    role: str,
    settings: Settings,
) -> tuple[str, str]:
    """Returns ``(token, jti)``. The ``jti`` is what logout denylists."""
    now = datetime.now(UTC)
    jti = str(uuid.uuid4())
    claims: dict[str, Any] = {
        "sub": str(user_id),
        "role": role,
        "jti": jti,
        "iat": now,
        "exp": now + timedelta(seconds=settings.access_token_ttl_seconds),
    }
    if tenant_id is not None:
        claims["tid"] = str(tenant_id)
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
        return TokenClaims(
            user_id=uuid.UUID(claims["sub"]),
            tenant_id=uuid.UUID(tid) if tid is not None else None,
            role=claims["role"],
            jti=claims["jti"],
        )
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise UnauthorizedError("The access token is invalid or has expired.") from exc
