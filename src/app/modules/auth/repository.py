"""DB access for refresh-token sessions and linked OAuth identities.

``tenant_id`` scopes every method (golden rule §5); ``None`` selects the
platform-staff partition, mirroring the identity-RLS policy underneath.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AuthSession, OAuthIdentity


def _tenant_clause(tenant_id: uuid.UUID | None) -> ColumnElement[bool]:
    if tenant_id is None:
        return AuthSession.tenant_id.is_(None)
    return AuthSession.tenant_id == tenant_id


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_token_hash(
        self, tenant_id: uuid.UUID | None, token_hash: str
    ) -> AuthSession | None:
        stmt = select(AuthSession).where(
            _tenant_clause(tenant_id), AuthSession.refresh_token_hash == token_hash
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def revoke_family(self, tenant_id: uuid.UUID | None, family_id: uuid.UUID) -> None:
        stmt = (
            update(AuthSession)
            .where(
                _tenant_clause(tenant_id),
                AuthSession.family_id == family_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        await self.session.execute(stmt)

    async def revoke_all_for_user(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID) -> None:
        stmt = (
            update(AuthSession)
            .where(
                _tenant_clause(tenant_id),
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=datetime.now(UTC))
        )
        await self.session.execute(stmt)

    async def get(self, tenant_id: uuid.UUID | None, session_id: uuid.UUID) -> AuthSession | None:
        stmt = select(AuthSession).where(_tenant_clause(tenant_id), AuthSession.id == session_id)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_active_for_user(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> list[AuthSession]:
        """Live sessions for the session-list endpoint (§10.3), newest first.

        Revoked and expired rows are excluded: the list exists so a person can
        answer "where am I still signed in", and a dead row is only noise.
        """
        stmt = (
            select(AuthSession)
            .where(
                _tenant_clause(tenant_id),
                AuthSession.user_id == user_id,
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > datetime.now(UTC),
            )
            .order_by(AuthSession.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    def add(self, session_row: AuthSession) -> None:
        self.session.add(session_row)

    async def flush(self) -> None:
        await self.session.flush()


class OAuthIdentityRepository:
    """Links between local accounts and external identity providers (§7.1)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_subject(
        self, tenant_id: uuid.UUID | None, provider: str, subject: str
    ) -> OAuthIdentity | None:
        clause = (
            OAuthIdentity.tenant_id.is_(None)
            if tenant_id is None
            else OAuthIdentity.tenant_id == tenant_id
        )
        stmt = select(OAuthIdentity).where(
            clause, OAuthIdentity.provider == provider, OAuthIdentity.subject == subject
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    def add(self, identity: OAuthIdentity) -> None:
        self.session.add(identity)

    async def flush(self) -> None:
        await self.session.flush()
