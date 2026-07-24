"""DB access for the compliance module (§8.17). Every method takes
``tenant_id`` (golden rule §5); all three tables are tenant-owned and
RLS-protected (migration 0022)."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.compliance.models import (
    ConsentCategory,
    ConsentRecord,
    CookieConsentConfig,
    DsrKind,
    DsrRequest,
    DsrStatus,
)


class ComplianceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def add(self, obj: object) -> None:
        self.session.add(obj)

    async def flush(self) -> None:
        await self.session.flush()

    # ---- consent records (append-only) ----

    async def list_consent_for_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> list[ConsentRecord]:
        stmt = (
            select(ConsentRecord)
            .where(ConsentRecord.tenant_id == tenant_id, ConsentRecord.user_id == user_id)
            .order_by(ConsentRecord.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars())

    async def latest_consent_for_session(
        self, tenant_id: uuid.UUID, session_id: str, category: ConsentCategory
    ) -> ConsentRecord | None:
        """The most recent consent record for a (session, category) — the
        analytics gate reads this to decide whether a cookie-bound session
        consented to analytics tracking."""
        stmt = (
            select(ConsentRecord)
            .where(
                ConsentRecord.tenant_id == tenant_id,
                ConsentRecord.session_id == session_id,
                ConsentRecord.category == category,
            )
            .order_by(ConsentRecord.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def latest_consent_for_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, category: ConsentCategory
    ) -> ConsentRecord | None:
        stmt = (
            select(ConsentRecord)
            .where(
                ConsentRecord.tenant_id == tenant_id,
                ConsentRecord.user_id == user_id,
                ConsentRecord.category == category,
            )
            .order_by(ConsentRecord.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalars().first()

    # ---- cookie-consent config (one per tenant) ----

    async def get_cookie_config(self, tenant_id: uuid.UUID) -> CookieConsentConfig | None:
        stmt = select(CookieConsentConfig).where(CookieConsentConfig.tenant_id == tenant_id)
        return (await self.session.execute(stmt)).scalars().first()

    # ---- DSR requests ----

    async def get_dsr(self, tenant_id: uuid.UUID, dsr_id: uuid.UUID) -> DsrRequest | None:
        stmt = select(DsrRequest).where(DsrRequest.tenant_id == tenant_id, DsrRequest.id == dsr_id)
        return (await self.session.execute(stmt)).scalars().first()

    async def pending_erasure_for_user(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID
    ) -> DsrRequest | None:
        stmt = select(DsrRequest).where(
            DsrRequest.tenant_id == tenant_id,
            DsrRequest.user_id == user_id,
            DsrRequest.kind == DsrKind.ERASURE,
            DsrRequest.status == DsrStatus.PENDING,
        )
        return (await self.session.execute(stmt)).scalars().first()

    async def list_erasures_due(self, *, now: datetime) -> list[DsrRequest]:
        """Pending erasure requests whose 30-day purge is due — the purge sweep
        driver. Not tenant-scoped: the sweep runs once across every tenant (RLS
        is set per-tenant by the worker when it acts on each row's tenant)."""
        stmt = select(DsrRequest).where(
            DsrRequest.kind == DsrKind.ERASURE,
            DsrRequest.status == DsrStatus.PENDING,
            DsrRequest.purge_scheduled_at.is_not(None),
            DsrRequest.purge_scheduled_at <= now,
        )
        return list((await self.session.execute(stmt)).scalars())
