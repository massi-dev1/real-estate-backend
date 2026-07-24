"""Compliance business logic (§8.17): consent records, cookie-consent config,
data-subject requests, and the analytics consent gate.

Everything here is a **thin orchestrator over other modules' boundary methods**
— the DSR export/erasure fan-out reads and writes through
``LeadsService`` / ``FavoritesService`` / ``NotificationsService`` /
``UserService``, never their tables (§5). The only tables this module owns are
its own three (consent records, cookie config, DSR requests).

Design notes:

- **Consent is append-only proof** (§10.12): recording a choice always inserts a
  new ``ConsentRecord``; a withdrawal is a new row with ``granted = false``, so
  the trail shows the whole history. The write path is called from every place
  consent is collected — the public cookie banner and the saved-search
  double-opt-in.
- **Erasure keeps business records, anonymizes people.** A closed deal's
  commission figure and a lost lead's pipeline row are legitimate records an
  agency must keep; the *person* is removed (contact PII stripped, account
  tombstoned). A buyer's favorites and saved searches are pure preference rows
  with no such value, so they're hard-deleted. Judgement is documented per data
  type inline.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.tenancy import TenantContext
from app.modules.compliance.models import (
    ConsentCategory,
    ConsentRecord,
    CookieConsentConfig,
    DsrKind,
    DsrRequest,
    DsrStatus,
)
from app.modules.compliance.repository import ComplianceRepository
from app.modules.compliance.schemas import (
    ConsentIn,
    CookieConsentConfigIn,
)
from app.modules.favorites.service import FavoritesService, build_favorites_boundary
from app.modules.leads.service import LeadsService, get_leads_service
from app.modules.notifications.service import (
    NotificationsService,
    build_notifications_boundary,
)
from app.modules.users.service import UserService, get_user_service

logger = structlog.get_logger(__name__)

# Erasure grace period before the hard purge runs (§10.12). A soft-delete now,
# a 30-day window to cancel/recover, then anonymization.
ERASURE_PURGE_DELAY_DAYS = 30

# Lost leads are anonymized after this long without activity (§8.17 retention).
LOST_LEAD_RETENTION_DAYS = 730  # 24 months


class ConsentRecorder:
    """A minimal append-only consent writer other capture flows compose in
    (§8.17 "wire the write path everywhere consent is collected"). It touches
    only the compliance repository — no other module's service — so a caller
    (e.g. favorites' saved-search opt-in) can record a consent record without
    pulling the full ``ComplianceService`` boundary graph or risking a cycle."""

    def __init__(self, repo: ComplianceRepository) -> None:
        self.repo = repo

    async def record(
        self,
        tenant: TenantContext,
        *,
        category: ConsentCategory,
        granted: bool,
        source: str,
        user_id: uuid.UUID | None = None,
        email: str | None = None,
        session_id: str | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> ConsentRecord:
        record = ConsentRecord(
            tenant_id=tenant.id,
            user_id=user_id,
            email=email,
            session_id=session_id,
            category=category,
            granted=granted,
            source=source,
            ip=ip,
            user_agent=user_agent,
        )
        self.repo.add(record)
        await self.repo.flush()
        return record


def build_consent_recorder(session: AsyncSession) -> ConsentRecorder:
    """Boundary factory for dependent capture flows (favorites' saved-search
    double-opt-in, §8.9/§8.17)."""
    return ConsentRecorder(ComplianceRepository(session))


class ConsentGate:
    """The read side of the consent boundary: does a session/user permit a
    category? Analytics ingestion composes this to honour a cookie-consent
    choice (§8.15 — the TODO Part 21 left). Touches only the compliance
    repository, so no cross-module cycle (analytics → compliance is one-way)."""

    def __init__(self, repo: ComplianceRepository) -> None:
        self.repo = repo

    async def analytics_allowed(
        self,
        tenant: TenantContext,
        *,
        user_id: uuid.UUID | None,
        session_id: str | None,
    ) -> bool:
        """Does this subject permit analytics tracking?

        - A fully anonymous hit with **no** session id can't be tied to a
          consent choice, so it's allowed (nothing cookie-bound to gate — the
          event carries no identity). Matches §8.15's "anonymous firehose" while
          still honouring an explicit opt-out.
        - An authenticated hit is allowed unless the user's latest ANALYTICS
          consent record is a rejection.
        - A session-bound (cookie) hit is allowed only if that session's latest
          ANALYTICS record is ``granted`` — privacy-first: a session that
          identifies itself but hasn't consented is blocked until it does.
        """
        if user_id is not None:
            record = await self.repo.latest_consent_for_user(
                tenant.id, user_id, ConsentCategory.ANALYTICS
            )
            return record is None or record.granted
        if session_id:
            record = await self.repo.latest_consent_for_session(
                tenant.id, session_id, ConsentCategory.ANALYTICS
            )
            return record is not None and record.granted
        return True


def build_consent_gate(session: AsyncSession) -> ConsentGate:
    """Boundary factory for analytics ingestion's per-session consent gate."""
    return ConsentGate(ComplianceRepository(session))


class ComplianceService:
    def __init__(
        self,
        repo: ComplianceRepository,
        users: UserService,
        leads: LeadsService,
        favorites: FavoritesService,
        notifications: NotificationsService,
    ) -> None:
        self.repo = repo
        self.users = users
        self.leads = leads
        self.favorites = favorites
        self.notifications = notifications

    # ---- consent recording (append-only) ----

    async def record_consent(
        self,
        tenant: TenantContext,
        *,
        category: ConsentCategory,
        granted: bool,
        source: str,
        user_id: uuid.UUID | None = None,
        email: str | None = None,
        session_id: str | None = None,
        legal_page_id: uuid.UUID | None = None,
        legal_version: int | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> ConsentRecord:
        """Append one consent record (§10.12). The single write path every
        consent-collection site routes through — the cookie banner, the
        saved-search opt-in, and any future legal-acceptance flow."""
        record = ConsentRecord(
            tenant_id=tenant.id,
            user_id=user_id,
            email=email,
            session_id=session_id,
            category=category,
            granted=granted,
            source=source,
            legal_page_id=legal_page_id,
            legal_version=legal_version,
            ip=ip,
            user_agent=user_agent,
        )
        self.repo.add(record)
        await self.repo.flush()
        return record

    async def record_banner_choices(
        self,
        tenant: TenantContext,
        data: ConsentIn,
        *,
        user_id: uuid.UUID | None,
        ip: str | None,
        user_agent: str | None,
    ) -> list[ConsentRecord]:
        """Public cookie-banner submission → one record per category choice.
        An anonymous submission must carry a ``session_id`` (otherwise the
        record could never be tied back to the browsing session it governs)."""
        if user_id is None and not data.session_id:
            raise ConflictError("A session id is required for anonymous consent.")
        records = [
            await self.record_consent(
                tenant,
                category=category,
                granted=granted,
                source="cookie_banner",
                user_id=user_id,
                session_id=data.session_id,
                ip=ip,
                user_agent=user_agent,
            )
            for category, granted in data.choices.items()
        ]
        return records

    # ---- cookie-consent config (portal, §8.17) ----

    async def get_cookie_config(self, tenant: TenantContext) -> CookieConsentConfig | None:
        return await self.repo.get_cookie_config(tenant.id)

    async def put_cookie_config(
        self, tenant: TenantContext, data: CookieConsentConfigIn
    ) -> CookieConsentConfig:
        """Upsert the tenant's cookie-banner config (full replacement)."""
        config = await self.repo.get_cookie_config(tenant.id)
        if config is None:
            config = CookieConsentConfig(tenant_id=tenant.id)
            self.repo.add(config)
        config.categories = data.categories
        config.banner_copy = data.banner_copy
        config.is_enabled = data.is_enabled
        await self.repo.flush()
        return config

    # ---- data-subject requests: export (§10.12) ----

    async def export_for_user(self, tenant: TenantContext, user_id: uuid.UUID) -> dict[str, object]:
        """Aggregate the subject's data read-only across every module holding a
        ``user_id`` / ``contact_id`` for them (§10.12). Each section is one
        module's own boundary dump — never a raw cross-module table read."""
        account = await self.users.export_identity(tenant.id, user_id)
        email = account["email"] if account else None
        sections: dict[str, object] = {"account": account}
        # CRM footprint is keyed on email (a portal account ↔ its CRM contact).
        if isinstance(email, str):
            sections["crm"] = await self.leads.export_for_subject(tenant.id, email)
        sections["favorites"] = await self.favorites.export_for_user(tenant, user_id)
        sections["notifications"] = await self.notifications.export_for_user(tenant, user_id)
        # Consent history is compliance's own — proof the subject can see.
        consent = await self.repo.list_consent_for_user(tenant.id, user_id)
        sections["consent"] = [
            {
                "category": c.category.value,
                "granted": c.granted,
                "source": c.source,
                "created_at": c.created_at.isoformat(),
            }
            for c in consent
        ]
        return {
            "subject_user_id": str(user_id),
            "subject_email": email,
            "exported_at": datetime.now(UTC).isoformat(),
            "sections": sections,
        }

    # ---- data-subject requests: erasure (§10.12) ----

    async def request_erasure(
        self, tenant: TenantContext, user_id: uuid.UUID, *, ip: str | None
    ) -> DsrRequest:
        """``DELETE /me``: soft-delete the account now and schedule the 30-day
        purge (§10.12). Idempotent — a second call while one is pending returns
        the existing request rather than starting another."""
        existing = await self.repo.pending_erasure_for_user(tenant.id, user_id)
        if existing is not None:
            return existing
        identity = await self.users.soft_delete_self(tenant.id, user_id)
        now = datetime.now(UTC)
        request = DsrRequest(
            tenant_id=tenant.id,
            user_id=user_id,
            subject_email=identity.email,
            kind=DsrKind.ERASURE,
            status=DsrStatus.PENDING,
            purge_scheduled_at=now + timedelta(days=ERASURE_PURGE_DELAY_DAYS),
            ip=ip,
        )
        self.repo.add(request)
        await self.repo.flush()
        logger.info(
            "dsr_erasure_requested",
            tenant_id=str(tenant.id),
            user_id=str(user_id),
            purge_at=request.purge_scheduled_at.isoformat() if request.purge_scheduled_at else None,
        )
        return request

    async def execute_erasure(self, tenant: TenantContext, dsr: DsrRequest) -> dict[str, int]:
        """Carry out a due erasure request (the purge sweep drives this). Fans
        out per data type with the documented anonymize-vs-delete judgement:

        - **CRM contacts** → anonymized (leads/deals are business records; the
          person is stripped, the pipeline shape kept).
        - **Account row** → tombstoned (FKs across the app point at it).
        - **Favorites + saved searches** → hard-deleted (pure preferences).
        - **Notifications** → deleted (transient in-app messages).

        Idempotent: re-running against an already-anonymized subject is a no-op
        in effect (each boundary skips already-scrubbed rows)."""
        assert dsr.user_id is not None
        result: dict[str, int] = {}
        # 1. Tombstone the account and recover its email to find CRM contacts.
        original_email = await self.users.anonymize_account(tenant.id, dsr.user_id)
        result["account_anonymized"] = 1 if original_email else 0
        # 2. Anonymize CRM contacts tied to that email (keep leads/activities).
        email = original_email or dsr.subject_email
        if email:
            result["contacts_anonymized"] = await self.leads.anonymize_subject(tenant.id, email)
        # 3. Hard-delete preference rows.
        await self.favorites.erase_for_user(tenant, dsr.user_id)
        await self.notifications.erase_for_user(tenant, dsr.user_id)
        # 4. Stamp the request done.
        dsr.status = DsrStatus.COMPLETED
        dsr.completed_at = datetime.now(UTC)
        dsr.result = dict(result)
        await self.repo.flush()
        logger.info(
            "dsr_erasure_executed",
            tenant_id=str(tenant.id),
            user_id=str(dsr.user_id),
            **result,
        )
        return result

    async def get_dsr(self, tenant: TenantContext, dsr_id: uuid.UUID) -> DsrRequest:
        dsr = await self.repo.get_dsr(tenant.id, dsr_id)
        if dsr is None:
            raise NotFoundError("Data-subject request not found.")
        return dsr

    # ---- retention (§8.17) ----

    async def anonymize_stale_lost_leads(
        self, tenant: TenantContext, *, now: datetime | None = None
    ) -> int:
        """Anonymize contacts of LOST leads untouched for 24 months. Delegates
        to the leads boundary — compliance never reaches into leads' tables."""
        now = now or datetime.now(UTC)
        cutoff = now - timedelta(days=LOST_LEAD_RETENTION_DAYS)
        return await self.leads.anonymize_lost_leads(tenant.id, before=cutoff)


def _build(session: AsyncSession, redis: Redis | None) -> ComplianceService:
    return ComplianceService(
        ComplianceRepository(session),
        get_user_service(session),
        get_leads_service(session),
        build_favorites_boundary(session),
        build_notifications_boundary(session, redis=redis),
    )


def get_compliance_service(session: SessionDep, request: Request) -> ComplianceService:
    return _build(session, request.app.state.redis)


def build_compliance_service_for_worker(session: AsyncSession) -> ComplianceService:
    """Worker-side construction (no ``request``): the retention/purge sweeps
    only touch tenant-owned tables through boundary methods; the live WS push
    (which needs redis) never fires on these paths."""
    return _build(session, None)


ComplianceServiceDep = Annotated[ComplianceService, Depends(get_compliance_service)]
