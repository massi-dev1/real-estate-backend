"""DB access for refresh-token sessions.

``tenant_id`` scopes every method (golden rule §5); ``None`` selects the
platform-staff partition, mirroring the identity-RLS policy underneath.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.auth.models import AuthSession


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

    def add(self, session_row: AuthSession) -> None:
        self.session.add(session_row)

    async def flush(self) -> None:
        await self.session.flush()
