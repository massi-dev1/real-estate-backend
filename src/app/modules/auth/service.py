"""Auth business logic (§7.1): sessions, token rotation with reuse detection,
password reset and email verification.

Identity data lives in the users module and is reached only through its
service (module-boundary rule §5). This module owns the session table, the
refresh-token lifecycle and the single-use Redis tokens:

- ``auth:reset:{sha256}``  → user id (TTL 30 min)
- ``auth:verify:{sha256}`` → user id (TTL 24 h)
- ``auth:jti:deny:{jti}``  → logout denylist (TTL = access-token lifetime)

Deliberately deferred (§7.1, later hardening part): MFA/TOTP, OAuth,
account-lockout backoff, breached-password checks, session-list endpoint.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.database import SessionDep, set_tenant_guc
from app.core.exceptions import UnauthorizedError
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_token,
    jti_denylist_key,
    user_jtis_key,
)
from app.integrations.email.service import EmailMessage, EmailService
from app.modules.auth.models import AuthSession
from app.modules.auth.repository import SessionRepository
from app.modules.auth.schemas import LoginRequest, RegisterRequest
from app.modules.users.service import UserIdentity, UserService, get_user_service

logger = structlog.get_logger(__name__)

_RESET_KEY = "auth:reset:{}"
_VERIFY_KEY = "auth:verify:{}"


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    user: UserIdentity
    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class ClientInfo:
    user_agent: str | None
    ip: str | None


def client_info(request: Request) -> ClientInfo:
    user_agent = request.headers.get("user-agent")
    return ClientInfo(
        user_agent=user_agent[:400] if user_agent else None,
        ip=request.client.host if request.client else None,
    )


class AuthService:
    def __init__(
        self,
        users: UserService,
        sessions: SessionRepository,
        redis: Redis,
        email: EmailService,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self.users = users
        self.sessions = sessions
        self.redis = redis
        self.email = email
        self.settings = settings
        self.session_factory = session_factory

    # ---- registration / login ----

    async def register(
        self, tenant_id: uuid.UUID, data: RegisterRequest, client: ClientInfo
    ) -> IssuedTokens:
        identity = await self.users.register_account(
            tenant_id,
            email=data.email,
            password=data.password,
            role=data.role,
            first_name=data.first_name,
            last_name=data.last_name,
            locale=data.locale,
            phone=data.phone,
        )
        await self.send_email_verification(identity)
        return await self._issue(identity, client, family_id=None)

    async def login(
        self, tenant_id: uuid.UUID | None, data: LoginRequest, client: ClientInfo
    ) -> IssuedTokens:
        identity = await self.users.verify_credentials(tenant_id, data.email, data.password)
        if identity is None:
            # One generic message for unknown email / wrong password / disabled.
            raise UnauthorizedError("Invalid email or password.")
        return await self._issue(identity, client, family_id=None)

    async def _issue(
        self, identity: UserIdentity, client: ClientInfo, *, family_id: uuid.UUID | None
    ) -> IssuedTokens:
        refresh_token = generate_refresh_token()
        self.sessions.add(
            AuthSession(
                user_id=identity.id,
                tenant_id=identity.tenant_id,
                refresh_token_hash=hash_token(refresh_token),
                family_id=family_id or uuid.uuid4(),
                user_agent=client.user_agent,
                ip=client.ip,
                expires_at=datetime.now(UTC)
                + timedelta(days=self.settings.refresh_token_ttl_days),
            )
        )
        await self.sessions.flush()
        access_token, jti = create_access_token(
            user_id=identity.id,
            tenant_id=identity.tenant_id,
            role=identity.role.value,
            settings=self.settings,
        )
        await self._track_jti(identity.id, jti)
        return IssuedTokens(user=identity, access_token=access_token, refresh_token=refresh_token)

    # ---- refresh rotation with reuse detection ----

    async def refresh(
        self, tenant_id: uuid.UUID | None, presented_token: str | None, client: ClientInfo
    ) -> IssuedTokens:
        if not presented_token:
            raise UnauthorizedError("A refresh token is required.")
        session = await self.sessions.get_by_token_hash(tenant_id, hash_token(presented_token))
        if session is None:
            raise UnauthorizedError("The refresh token is invalid.")

        if session.revoked_at is not None:
            # A rotated-away token came back: theft indicator (§7.1). Kill the
            # whole family so neither the thief nor the victim keeps a session.
            logger.warning(
                "refresh_token_reuse_detected",
                user_id=str(session.user_id),
                family_id=str(session.family_id),
            )
            await self._revoke_family_committed(tenant_id, session.family_id)
            raise UnauthorizedError("The refresh token is invalid.")

        if session.expires_at <= datetime.now(UTC):
            raise UnauthorizedError("The refresh token has expired.")

        identity = await self.users.get_identity_if_active(tenant_id, session.user_id)
        if identity is None:
            raise UnauthorizedError("The refresh token is invalid.")

        session.revoked_at = datetime.now(UTC)
        return await self._issue(identity, client, family_id=session.family_id)

    async def _revoke_family_committed(
        self, tenant_id: uuid.UUID | None, family_id: uuid.UUID
    ) -> None:
        """Revoke on a dedicated committed transaction: the 401 raised right
        after rolls back the request transaction, which would silently undo an
        in-request revocation."""
        async with self.session_factory() as own_session, own_session.begin():
            if tenant_id is not None:
                await set_tenant_guc(own_session, tenant_id)
            await SessionRepository(own_session).revoke_family(tenant_id, family_id)

    # ---- logout ----

    async def logout(
        self, tenant_id: uuid.UUID | None, presented_token: str | None, jti: str
    ) -> None:
        if presented_token:
            session = await self.sessions.get_by_token_hash(tenant_id, hash_token(presented_token))
            if session is not None and session.revoked_at is None:
                session.revoked_at = datetime.now(UTC)
                await self.sessions.flush()
        await self._denylist_jti(jti)

    async def logout_all(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, jti: str) -> None:
        await self.force_logout_user(tenant_id, user_id)
        # Belt and braces: the caller's own jti even if tracking missed it.
        await self._denylist_jti(jti)

    async def force_logout_user(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID) -> None:
        """Revoke *everything* a user holds: refresh sessions and live access
        tokens. Called on logout-all, password reset, and by the users module
        when an admin disables/demotes/deletes an account."""
        await self.sessions.revoke_all_for_user(tenant_id, user_id)
        await self._revoke_tracked_jtis(user_id)

    async def _track_jti(self, user_id: uuid.UUID, jti: str) -> None:
        """Remember a live jti so force-logout can denylist it. Fail-soft like
        the denylist itself: Redis loss shortens revocation reach, never auth."""
        try:
            pipe = self.redis.pipeline()
            pipe.sadd(user_jtis_key(user_id), jti)
            pipe.expire(user_jtis_key(user_id), self.settings.access_token_ttl_seconds)
            await pipe.execute()
        except Exception:
            logger.warning("jti_track_failed", user_id=str(user_id))

    async def _revoke_tracked_jtis(self, user_id: uuid.UUID) -> None:
        try:
            key = user_jtis_key(user_id)
            jtis = await self.redis.smembers(key)
            if not jtis:
                return
            pipe = self.redis.pipeline()
            for jti in jtis:
                jti_str = jti if isinstance(jti, str) else jti.decode()
                pipe.set(
                    jti_denylist_key(jti_str), "1", ex=self.settings.access_token_ttl_seconds
                )
            pipe.delete(key)
            await pipe.execute()
        except Exception:
            logger.warning("jti_revoke_all_failed", user_id=str(user_id))

    async def _denylist_jti(self, jti: str) -> None:
        """Denylist for the full access-token lifetime (a safe upper bound on
        the token's remaining validity). Redis loss here only shortens the
        denylist, never extends a session — the row revocations are committed."""
        try:
            await self.redis.set(
                jti_denylist_key(jti), "1", ex=self.settings.access_token_ttl_seconds
            )
        except Exception:
            logger.warning("jti_denylist_write_failed", jti=jti)

    # ---- password reset (single-use, hashed, 30 min — §7.1) ----

    async def forgot_password(self, tenant_id: uuid.UUID | None, email: str) -> None:
        """Always succeeds from the caller's perspective (202) — existence of
        an account must not be observable."""
        identity = await self.users.get_identity_by_email(tenant_id, email)
        if identity is None:
            return
        token = secrets.token_urlsafe(32)
        await self.redis.set(
            _RESET_KEY.format(hash_token(token)),
            str(identity.id),
            ex=self.settings.password_reset_ttl_seconds,
        )
        await self._send(
            identity.email,
            "Reset your password",
            f"Use this code to reset your password (valid 30 minutes):\n\ncode: {token}\n",
        )

    async def reset_password(
        self, tenant_id: uuid.UUID | None, token: str, new_password: str
    ) -> None:
        user_id = await self._consume_token(_RESET_KEY.format(hash_token(token)))
        identity = await self.users.set_password(tenant_id, user_id, new_password)
        if identity is None:
            raise UnauthorizedError("The reset token is invalid or has expired.")
        # A reset means the old credential can no longer be trusted anywhere.
        await self.force_logout_user(tenant_id, user_id)

    # ---- email verification (§7.1) ----

    async def resend_email_verification(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> None:
        identity = await self.users.get_identity_if_active(tenant_id, user_id)
        if identity is not None:
            await self.send_email_verification(identity)

    async def send_email_verification(self, identity: UserIdentity) -> None:
        if identity.email_verified_at is not None:
            return
        token = secrets.token_urlsafe(32)
        await self.redis.set(
            _VERIFY_KEY.format(hash_token(token)),
            str(identity.id),
            ex=self.settings.email_verification_ttl_seconds,
        )
        await self._send(
            identity.email,
            "Verify your email",
            f"Use this code to verify your email address:\n\ncode: {token}\n",
        )

    async def verify_email(self, tenant_id: uuid.UUID | None, token: str) -> None:
        user_id = await self._consume_token(_VERIFY_KEY.format(hash_token(token)))
        identity = await self.users.mark_email_verified(tenant_id, user_id)
        if identity is None:
            raise UnauthorizedError("The verification token is invalid or has expired.")

    async def _consume_token(self, key: str) -> uuid.UUID:
        """GETDEL makes single-use atomic — two concurrent redemptions cannot
        both succeed."""
        value = await self.redis.getdel(key)
        if value is None:
            raise UnauthorizedError("The token is invalid or has expired.")
        return uuid.UUID(value if isinstance(value, str) else value.decode())

    async def _send(self, to: str, subject: str, text: str) -> None:
        """Fail-soft: a down SMTP relay must not turn registration or the
        (deliberately opaque) forgot-password flow into a 500. Sends move to a
        Celery task in Part 5."""
        try:
            await self.email.send(EmailMessage(to=to, subject=subject, text=text))
        except Exception:
            logger.warning("auth_email_send_failed", subject=subject)


def get_auth_service(session: SessionDep, request: Request) -> AuthService:
    return AuthService(
        users=get_user_service(session),
        sessions=SessionRepository(session),
        redis=request.app.state.redis,
        email=request.app.state.email_service,
        settings=request.app.state.settings,
        session_factory=request.app.state.session_factory,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
