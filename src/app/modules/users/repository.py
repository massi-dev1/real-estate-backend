"""DB access for users.

Every method takes ``tenant_id`` (golden rule §5); ``None`` means the
platform-staff partition (``tenant_id IS NULL``) and is only ever passed by
platform-scoped services. Soft-deleted rows are excluded everywhere.
"""

import uuid
from datetime import datetime

from sqlalchemy import ColumnElement, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.modules.users.models import User, UserStatus


def _tenant_clause(tenant_id: uuid.UUID | None) -> ColumnElement[bool]:
    return User.tenant_id.is_(None) if tenant_id is None else User.tenant_id == tenant_id


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID) -> User | None:
        stmt = select(User).where(
            _tenant_clause(tenant_id), User.id == user_id, User.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def get_by_email(self, tenant_id: uuid.UUID | None, email: str) -> User | None:
        stmt = select(User).where(
            _tenant_clause(tenant_id), User.email == email, User.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_page(
        self,
        tenant_id: uuid.UUID | None,
        *,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[User]:
        """Keyset page ordered by (created_at, id) descending; fetches limit+1
        rows so the caller can tell whether a next page exists."""
        stmt = (
            select(User)
            .where(_tenant_clause(tenant_id), User.deleted_at.is_(None))
            .order_by(User.created_at.desc(), User.id.desc())
            .limit(limit + 1)
        )
        if after is not None:
            stmt = stmt.where(tuple_(User.created_at, User.id) < after)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count(self, tenant_id: uuid.UUID | None) -> int:
        stmt = select(func.count(User.id)).where(
            _tenant_clause(tenant_id), User.deleted_at.is_(None)
        )
        return (await self.session.execute(stmt)).scalar_one()

    async def list_active_by_role(self, tenant_id: uuid.UUID, role: Role) -> list[User]:
        """Deterministic order (by id) so callers needing a stable pool —
        e.g. round-robin lead assignment — get consistent tie-breaking."""
        stmt = (
            select(User)
            .where(
                User.tenant_id == tenant_id,
                User.role == role,
                User.status == UserStatus.ACTIVE,
                User.deleted_at.is_(None),
            )
            .order_by(User.id)
        )
        return list((await self.session.execute(stmt)).scalars())

    async def list_active_by_ids(
        self, tenant_id: uuid.UUID, user_ids: list[uuid.UUID]
    ) -> list[User]:
        if not user_ids:
            return []
        stmt = select(User).where(
            User.tenant_id == tenant_id,
            User.id.in_(user_ids),
            User.status == UserStatus.ACTIVE,
            User.deleted_at.is_(None),
        )
        return list((await self.session.execute(stmt)).scalars())

    def add(self, user: User) -> None:
        self.session.add(user)

    async def flush(self) -> None:
        await self.session.flush()
