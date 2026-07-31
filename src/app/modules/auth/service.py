"""Auth business logic (§7.1, §10.3): sessions, token rotation with reuse
detection, password reset, email verification, account lockout, MFA/TOTP and
the OAuth seam.

Identity data lives in the users module and is reached only through its
service (module-boundary rule §5) — the password hash and the TOTP secret
never cross that boundary. This module owns the session table, the OAuth
identity links, the refresh-token lifecycle and the Redis-backed tokens:

- ``auth:reset:{sha256}``   → user id (TTL 30 min)
- ``auth:verify:{sha256}``  → user id (TTL 24 h)
- ``auth:jti:deny:{jti}``   → logout denylist (TTL = access-token lifetime)
- ``auth:mfa:{sha256}``     → pending second-factor ticket (TTL 5 min)
- ``auth:oauth:{sha256}``   → OAuth CSRF state (TTL 10 min)
- ``auth:lockout:*``        → failed-login counters (``core.lockout``)

Deliberately deferred: passkeys/WebAuthn, SMS as a second factor (no adapter
exists, §8.12), and a live OAuth client (credential-gated).
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
from app.core.exceptions import (
    BreachedPasswordError,
    ConflictError,
    FeatureNotConfiguredError,
    NotFoundError,
    UnauthorizedError,
)
from app.core.lockout import LoginLockout
from app.core.permissions import Role
from app.core.rate_limit import client_ip
from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_token,
    jti_denylist_key,
    user_jtis_key,
)
from app.integrations.auth_oauth.base import OAuthError, OAuthProfile, OAuthProvider
from app.integrations.auth_oauth.registry import build_oauth_provider
from app.integrations.breach.hibp import build_breach_checker
from app.modules.auth.models import AuthSession, OAuthIdentity
from app.modules.auth.repository import OAuthIdentityRepository, SessionRepository
from app.modules.auth.schemas import LoginRequest, RegisterRequest
from app.modules.users.service import UserIdentity, UserService, get_user_service
from app.workers.tasks.email import send_email

logger = structlog.get_logger(__name__)

_RESET_KEY = "auth:reset:{}"
_VERIFY_KEY = "auth:verify:{}"
_MFA_KEY = "auth:mfa:{}"
_OAUTH_STATE_KEY = "auth:oauth:{}"
_OAUTH_STATE_TTL_SECONDS = 600

# Roles for which a verified second factor is mandatory once enrolled, and
# which are prompted to enrol (§7.1). These are the accounts that can publish
# listings, move money and read the whole tenant's CRM; a buyer's favourites
# list does not warrant forcing a hardware step on every visitor.
MFA_ENFORCED_ROLES: frozenset[Role] = frozenset({Role.ADMIN, Role.TEAM_LEAD, Role.AGENT})


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    user: UserIdentity
    access_token: str
    refresh_token: str


@dataclass(frozen=True, slots=True)
class MfaChallenge:
    """A login that passed the password but still owes a second factor."""

    mfa_token: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class ClientInfo:
    user_agent: str | None
    ip: str | None


def client_info(request: Request) -> ClientInfo:
    """The caller's user agent and address, for the session row and the per-IP
    lockout counter (§7.1).

    The address goes through :func:`app.core.rate_limit.client_ip`, so behind
    the §16 Caddy topology it is the real client rather than the proxy — the
    per-IP lockout key is the anti-spray backstop, and pooling every request
    under one proxy address would make it both useless (one attacker cannot be
    isolated) and dangerous (one attacker locks out everyone).
    """
    user_agent = request.headers.get("user-agent")
    return ClientInfo(
        user_agent=user_agent[:400] if user_agent else None,
        ip=client_ip(request, request.app.state.settings),
    )


class AuthService:
    def __init__(
        self,
        users: UserService,
        sessions: SessionRepository,
        redis: Redis,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        oauth_identities: OAuthIdentityRepository | None = None,
    ) -> None:
        self.users = users
        self.sessions = sessions
        self.redis = redis
        self.settings = settings
        self.session_factory = session_factory
        self.oauth_identities = oauth_identities or OAuthIdentityRepository(sessions.session)
        self.lockout = LoginLockout(redis, settings)
        self.breach = build_breach_checker(settings)

    # ---- password policy (§10.3) ----

    async def _reject_breached_password(self, password: str) -> None:
        """Refuse a password known to be in a public breach corpus.

        422 rather than 401/409: the payload *is* the problem and the caller
        can fix it by choosing differently, which is exactly what a validation
        error means here. Fail-open on an HIBP outage is handled inside the
        checker (documented there) — this never blocks on a third party.
        """
        if await self.breach.is_breached(password):
            raise BreachedPasswordError(
                "This password has appeared in a known data breach. Please choose a different one."
            )

    # ---- registration / login ----

    async def register(
        self, tenant_id: uuid.UUID, data: RegisterRequest, client: ClientInfo
    ) -> IssuedTokens:
        await self._reject_breached_password(data.password)
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
    ) -> IssuedTokens | MfaChallenge:
        """Password step. Returns tokens, or an :class:`MfaChallenge` when the
        account owes a second factor (§7.1).

        Every failure path — locked out, unknown email, wrong password,
        disabled account — raises the *same* generic 401. A distinct "account
        locked" response would confirm the address exists and tell an attacker
        their run is working; the lockout is meant to be felt as slowness, not
        read as a signal.
        """
        lock_scope = str(tenant_id) if tenant_id is not None else "platform"
        ip = client.ip or "unknown"
        if await self.lockout.is_locked(lock_scope, data.email, ip):
            logger.info("login_blocked_by_lockout", tenant_id=lock_scope)
            raise UnauthorizedError("Invalid email or password.")

        identity = await self.users.verify_credentials(tenant_id, data.email, data.password)
        if identity is None:
            await self.lockout.record_failure(lock_scope, data.email, ip)
            # One generic message for unknown email / wrong password / disabled.
            raise UnauthorizedError("Invalid email or password.")

        # The password was right, so this source and account are not under a
        # (successful) attack — clear the counters before anything else, or a
        # person who mistyped four times would stay one slip from a lockout.
        await self.lockout.reset(lock_scope, data.email, ip)

        if self._mfa_required(identity):
            return await self._start_mfa_challenge(identity)
        return await self._issue(identity, client, family_id=None)

    def _mfa_required(self, identity: UserIdentity) -> bool:
        """A factor is demanded only when one actually exists on the account.

        Enforcement for privileged roles is a *prompt to enrol* (surfaced by
        ``/auth/mfa/status``), not a hard login block: flipping the setting on
        would otherwise instantly lock out every existing admin, including the
        one who would have to fix it.
        """
        return identity.mfa_enabled

    async def _start_mfa_challenge(self, identity: UserIdentity) -> MfaChallenge:
        """Mint the short-lived ticket that the verify step consumes.

        Stored hashed (like every other Redis token here) and pinned to the
        tenant, so a ticket minted on agency A cannot be redeemed on B.
        """
        token = secrets.token_urlsafe(32)
        ttl = self.settings.mfa_pending_token_ttl_seconds
        tenant_part = str(identity.tenant_id) if identity.tenant_id else ""
        await self.redis.set(
            _MFA_KEY.format(hash_token(token)), f"{tenant_part}:{identity.id}", ex=ttl
        )
        return MfaChallenge(mfa_token=token, expires_in=ttl)

    async def verify_mfa(
        self, tenant_id: uuid.UUID | None, mfa_token: str, code: str, client: ClientInfo
    ) -> IssuedTokens:
        """Second step: redeem the ticket with a valid TOTP code.

        The ticket is consumed with ``GETDEL`` *before* the code is checked, so
        one ticket buys exactly one guess — otherwise a five-minute window
        would allow unlimited attempts at a six-digit code, which is a million
        guesses' worth of headroom.
        """
        raw = await self.redis.getdel(_MFA_KEY.format(hash_token(mfa_token)))
        if raw is None:
            raise UnauthorizedError("The verification session has expired. Please sign in again.")
        value = raw if isinstance(raw, str) else raw.decode()
        tenant_part, _, user_part = value.partition(":")
        expected_tenant = str(tenant_id) if tenant_id is not None else ""
        if tenant_part != expected_tenant:
            raise UnauthorizedError("The verification session is invalid.")

        user_id = uuid.UUID(user_part)
        if not await self.users.verify_mfa_code(tenant_id, user_id, code, self.settings):
            # The ticket is already spent: a wrong code costs a full re-login,
            # which bounds online guessing far more tightly than a rate limit.
            raise UnauthorizedError("That code is not valid. Please sign in again.")

        identity = await self.users.get_identity_if_active(tenant_id, user_id)
        if identity is None:
            raise UnauthorizedError("The verification session is invalid.")
        return await self._issue(identity, client, family_id=None)

    async def _issue(
        self, identity: UserIdentity, client: ClientInfo, *, family_id: uuid.UUID | None
    ) -> IssuedTokens:
        refresh_token = generate_refresh_token()
        now = datetime.now(UTC)
        self.sessions.add(
            AuthSession(
                user_id=identity.id,
                tenant_id=identity.tenant_id,
                refresh_token_hash=hash_token(refresh_token),
                family_id=family_id or uuid.uuid4(),
                user_agent=client.user_agent,
                ip=client.ip,
                last_used_at=now,
                expires_at=now + timedelta(days=self.settings.refresh_token_ttl_days),
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

        now = datetime.now(UTC)
        # Stamp the presented row as just-used before it is rotated away: this
        # is what gives a device a real "last active" time. The new row this
        # refresh mints starts at `created_at == last_used_at == now`, so the
        # value on whichever row is currently live always reflects the most
        # recent authentication on that device, not merely when it first
        # signed in.
        session.last_used_at = now
        session.revoked_at = now
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
            # redis-py<7 (pinned transitively by celery[redis], §12) types
            # `smembers` as a sync/async overload union; this client is async.
            jtis = await self.redis.smembers(key)  # type: ignore[misc]
            if not jtis:
                return
            pipe = self.redis.pipeline()
            for jti in jtis:
                jti_str = jti if isinstance(jti, str) else jti.decode()
                pipe.set(jti_denylist_key(jti_str), "1", ex=self.settings.access_token_ttl_seconds)
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
        # Checked before the token is consumed: a rejected password must not
        # burn the single-use reset code, or the person is locked out of their
        # own recovery flow by a typo in their *new* password.
        await self._reject_breached_password(new_password)
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

    # ---- session list / "log out other devices" (§10.3) ----

    async def list_sessions(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, *, presented_token: str | None
    ) -> list[tuple[AuthSession, bool]]:
        """Live sessions, each flagged with whether it is the caller's own.

        The flag comes from matching the *presented refresh token's* hash, not
        from the access token: the access jti is not stored on the session row,
        and the refresh token is what actually identifies a device's chain.
        """
        rows = await self.sessions.list_active_for_user(tenant_id, user_id)
        current_hash = hash_token(presented_token) if presented_token else None
        return [(row, row.refresh_token_hash == current_hash) for row in rows]

    async def revoke_session(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> None:
        """Kill one session by id. Scoped to the caller's own rows: a 404 for
        anyone else's session, so the endpoint is not an oracle for which
        session ids exist."""
        row = await self.sessions.get(tenant_id, session_id)
        if row is None or row.user_id != user_id:
            raise NotFoundError("Session not found.")
        if row.revoked_at is None:
            row.revoked_at = datetime.now(UTC)
            await self.sessions.flush()
        # Only the refresh chain dies here: the access token minted from it is
        # not identifiable from this row, and it expires within 15 minutes.
        # Killing *every* live token of the user is what `logout-all` is for.

    # ---- MFA (§7.1) ----

    async def begin_mfa_enrolment(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> tuple[str, str]:
        """Returns ``(provisioning_uri, secret)`` for the caller to render."""
        return await self.users.begin_mfa_enrolment(
            tenant_id, user_id, issuer=self.settings.mfa_issuer
        )

    async def confirm_mfa_enrolment(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, code: str
    ) -> None:
        if not await self.users.confirm_mfa_enrolment(tenant_id, user_id, code, self.settings):
            raise UnauthorizedError("That code is not valid. Please try again.")

    async def disable_mfa(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, password: str
    ) -> None:
        if not await self.users.check_password(tenant_id, user_id, password):
            raise UnauthorizedError("The password is incorrect.")
        await self.users.disable_mfa(tenant_id, user_id)

    # ---- OAuth social login (§7.1, seam) ----

    async def start_oauth(self, tenant_id: uuid.UUID | None, provider_key: str) -> tuple[str, str]:
        """Returns ``(authorization_url, state)``.

        ``state`` is a single-use CSRF nonce held in Redis and pinned to the
        tenant, so a callback cannot be replayed, nor a code obtained on one
        agency's flow redeemed on another's.
        """
        provider = self._oauth_provider(provider_key)
        state = secrets.token_urlsafe(24)
        tenant_part = str(tenant_id) if tenant_id is not None else ""
        await self.redis.set(
            _OAUTH_STATE_KEY.format(hash_token(state)),
            f"{tenant_part}:{provider_key}",
            ex=_OAUTH_STATE_TTL_SECONDS,
        )
        return provider.authorization_url(
            redirect_uri=self._oauth_redirect_uri(provider_key), state=state
        ), state

    async def complete_oauth(
        self,
        tenant_id: uuid.UUID,
        provider_key: str,
        *,
        code: str,
        state: str,
        client: ClientInfo,
    ) -> IssuedTokens:
        provider = self._oauth_provider(provider_key)
        stored = await self.redis.getdel(_OAUTH_STATE_KEY.format(hash_token(state)))
        if stored is None:
            raise UnauthorizedError("The sign-in attempt has expired. Please try again.")
        value = stored if isinstance(stored, str) else stored.decode()
        if value != f"{tenant_id}:{provider_key}":
            raise UnauthorizedError("The sign-in attempt is invalid.")

        try:
            profile = await provider.exchange_code(
                code=code, redirect_uri=self._oauth_redirect_uri(provider_key)
            )
        except OAuthError as exc:
            logger.warning("oauth_exchange_failed", provider=provider_key, permanent=exc.permanent)
            raise UnauthorizedError("Sign-in with this provider failed. Please try again.") from exc

        identity = await self._link_or_create_oauth_account(tenant_id, provider_key, profile)
        return await self._issue(identity, client, family_id=None)

    async def _link_or_create_oauth_account(
        self, tenant_id: uuid.UUID, provider_key: str, profile: OAuthProfile
    ) -> UserIdentity:
        """Resolve an external profile to a local account.

        Three cases, in order: an existing link wins outright; otherwise an
        account with the same address is linked — but **only if the provider
        says it verified that address**, since an unverified provider email is
        an account-takeover primitive (claim someone's address at the provider,
        sign in as them here); otherwise a new self-registration-tier account
        is created with an unusable random password (the person signs in
        socially, and can set a real password through the reset flow).
        """
        link = await self.oauth_identities.get_by_subject(tenant_id, provider_key, profile.subject)
        if link is not None:
            identity = await self.users.get_identity_if_active(tenant_id, link.user_id)
            if identity is None:
                raise UnauthorizedError("This account is no longer active.")
            return identity

        if not profile.email:
            raise ConflictError("The provider did not supply an email address.")

        existing = await self.users.get_identity_by_email(tenant_id, profile.email)
        if existing is not None and not profile.email_verified:
            raise ConflictError(
                "An account already exists for this email. Sign in with your password instead."
            )
        # `get_identity_by_email` only returns *active* accounts. If the address
        # is taken by an inactive (suspended/disabled) one, fall-through would
        # try to insert a duplicate and surface an opaque unique-violation 409;
        # give the real reason instead.
        if existing is None and await self.users.email_taken(tenant_id, profile.email):
            raise ConflictError("This account is not active. Contact your agency administrator.")

        identity = existing or await self.users.register_account(
            tenant_id,
            email=profile.email,
            password=secrets.token_urlsafe(32),
            role=Role.BUYER_RENTER,
            first_name=profile.first_name,
            last_name=profile.last_name,
        )
        self.oauth_identities.add(
            OAuthIdentity(
                user_id=identity.id,
                tenant_id=tenant_id,
                provider=provider_key,
                subject=profile.subject,
                email=profile.email,
            )
        )
        await self.oauth_identities.flush()
        if profile.email_verified:
            await self.users.mark_email_verified(tenant_id, identity.id)
        return identity

    def _oauth_provider(self, provider_key: str) -> OAuthProvider:
        provider = build_oauth_provider(self.settings, provider_key)
        if provider is None:
            raise FeatureNotConfiguredError(
                f"Social sign-in with '{provider_key}' is not configured on this deployment."
            )
        return provider

    def _oauth_redirect_uri(self, provider_key: str) -> str:
        base = self.settings.oauth_redirect_base_url.rstrip("/")
        return f"{base}/api/v1/auth/oauth/{provider_key}/callback"

    async def _send(self, to: str, subject: str, text: str) -> None:
        """Fail-soft: a broker hiccup must not turn registration or the
        (deliberately opaque) forgot-password flow into a 500. Delivery itself
        now runs off the request path entirely (§12) — this only enqueues."""
        try:
            send_email.delay(to=to, subject=subject, text=text)
        except Exception:
            logger.warning("auth_email_enqueue_failed", subject=subject)


def get_auth_service(session: SessionDep, request: Request) -> AuthService:
    return AuthService(
        users=get_user_service(session),
        sessions=SessionRepository(session),
        redis=request.app.state.redis,
        settings=request.app.state.settings,
        session_factory=request.app.state.session_factory,
    )


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
