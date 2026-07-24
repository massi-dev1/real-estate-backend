"""Minimal append-only audit trail (§10.11).

Added in Part 22 for the one use it requires — impersonation start (a platform
staff acting as a tenant user is a sensitive, must-be-logged action) — plus
tenant-lifecycle admin actions (suspend/activate/offboard/plan-change). The
table and this service are deliberately minimal; Part 23/compliance broadens
the write sites and adds the reporting/export surface on top of the same table.

``audit_log`` is a global (non-RLS) table, like the other platform tables.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.tenants.models import AuditLogEntry


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Who performed an audited action."""

    user_id: uuid.UUID | None
    role: str | None
    ip: str | None = None


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, entry: AuditLogEntry) -> None:
        self.session.add(entry)

    async def list_page(
        self,
        *,
        tenant_id: uuid.UUID | None,
        action: str | None,
        after: tuple[datetime, uuid.UUID] | None,
        limit: int,
    ) -> list[AuditLogEntry]:
        stmt = select(AuditLogEntry).order_by(
            AuditLogEntry.created_at.desc(), AuditLogEntry.id.desc()
        )
        if tenant_id is not None:
            stmt = stmt.where(AuditLogEntry.tenant_id == tenant_id)
        if action is not None:
            stmt = stmt.where(AuditLogEntry.action == action)
        if after is not None:
            stmt = stmt.where(tuple_(AuditLogEntry.created_at, AuditLogEntry.id) < after)
        return list((await self.session.execute(stmt.limit(limit + 1))).scalars().all())

    async def count(self, *, tenant_id: uuid.UUID | None, action: str | None) -> int:
        stmt = select(func.count(AuditLogEntry.id))
        if tenant_id is not None:
            stmt = stmt.where(AuditLogEntry.tenant_id == tenant_id)
        if action is not None:
            stmt = stmt.where(AuditLogEntry.action == action)
        return (await self.session.execute(stmt)).scalar_one()


class AuditService:
    def __init__(self, repo: AuditRepository) -> None:
        self.repo = repo

    def record(
        self,
        *,
        action: str,
        actor: AuditActor,
        tenant_id: uuid.UUID | None = None,
        target: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        """Append an audit entry. Not awaited beyond the add — it commits with
        the surrounding request transaction, so an audited action and its log
        entry are atomic (a rolled-back impersonation logs nothing)."""
        self.repo.add(
            AuditLogEntry(
                tenant_id=tenant_id,
                actor_user_id=actor.user_id,
                actor_role=actor.role,
                action=action,
                target=target,
                audit_metadata=metadata or {},
                ip=actor.ip,
            )
        )


def build_audit_service(session: AsyncSession) -> AuditService:
    return AuditService(AuditRepository(session))
