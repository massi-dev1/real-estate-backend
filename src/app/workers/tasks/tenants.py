"""Tenant lifecycle & billing Beat jobs (§8.16, §12).

- ``export_tenant`` — dump a tenant's data to a downloadable archive (offboard
  step 1). Enqueued post-commit when an offboard starts; runs scoped so RLS
  limits the dump to the tenant.
- ``purge_scheduled_tenants`` — hard-delete offboarded tenants past their
  scheduled-deletion instant (offboard step 2). Idempotent: the deleted-at
  stamp is the guard (same stance as ``stale_flagged_at``).
- ``run_dunning_sweep`` — suspend tenants whose past-due grace window closed.
- ``expire_trials`` — suspend trial tenants whose trial ended without a paid
  subscription.
- ``verify_pending_domains`` — re-check unverified custom domains' DNS TXT
  challenge.

Lifecycle sweeps run unscoped: they read/write the *global* platform tables
(tenants/subscriptions), not tenant-owned RLS rows. Only the export dump is
tenant-scoped.
"""

import uuid
from datetime import UTC, datetime

import structlog
from celery import shared_task
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.storage import create_storage
from app.modules.tenants.billing import build_billing_service
from app.modules.tenants.dns_verify import default_txt_lookup, txt_record_present
from app.modules.tenants.export import export_tenant_data
from app.modules.tenants.models import (
    DomainVerificationStatus,
    TenantDomain,
)
from app.modules.tenants.repository import TenantRepository
from app.workers.db import run_scoped

logger = structlog.get_logger(__name__)


@shared_task(name="app.workers.tasks.tenants.export_tenant")
def export_tenant(tenant_id: str) -> str:
    """Dump the tenant's rows to a private-bucket archive and stamp the object
    key on the tenant row. Runs scoped so RLS limits the dump to this tenant."""
    tid = uuid.UUID(tenant_id)
    storage = create_storage(get_settings())

    async def _export(session: AsyncSession) -> str:
        key = await export_tenant_data(session, tid, storage)
        # The tenants table is global (non-RLS); update it in the same scoped
        # transaction — the tenant_id GUC does not gate a non-RLS table.
        tenant = await TenantRepository(session).get(tid)
        if tenant is not None:
            tenant.export_object_key = key
        return key

    key = run_scoped(tid, _export)
    logger.info("tenant_exported", tenant_id=tenant_id, object_key=key)
    return key


@shared_task(name="app.workers.tasks.tenants.purge_scheduled_tenants")
def purge_scheduled_tenants() -> int:
    """Hard-delete offboarded tenants past their deletion instant (§8.16). The
    CASCADE FKs drop every owned row; ``deleted_at`` guards idempotency for the
    (rare) case a delete is retried after partial completion."""
    now = datetime.now(UTC)

    async def _purge(session: AsyncSession) -> int:
        repo = TenantRepository(session)
        due = await repo.list_deletions_due(now=now)
        for tenant in due:
            logger.info("tenant_purged", tenant_id=str(tenant.id), slug=tenant.slug)
            await repo.delete(tenant)
        return len(due)

    return run_scoped(None, _purge)


@shared_task(name="app.workers.tasks.tenants.run_dunning_sweep")
def run_dunning_sweep() -> int:
    """Suspend tenants whose dunning grace window has closed (§8.16)."""

    async def _sweep(session: AsyncSession) -> int:
        # A real Redis client so the status change invalidates the tenant cache
        # (a suspended tenant must stop resolving), matching the request path.
        redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            service = build_billing_service(session, redis)
            return await service.suspend_grace_expired()
        finally:
            await redis.aclose()

    return run_scoped(None, _sweep)


@shared_task(name="app.workers.tasks.tenants.expire_trials")
def expire_trials() -> int:
    """Suspend trial tenants whose trial ended without a paid subscription."""

    async def _sweep(session: AsyncSession) -> int:
        redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
        try:
            service = build_billing_service(session, redis)
            return await service.expire_trials()
        finally:
            await redis.aclose()

    return run_scoped(None, _sweep)


@shared_task(name="app.workers.tasks.tenants.verify_pending_domains")
def verify_pending_domains() -> int:
    """Re-check the DNS TXT challenge for every not-yet-verified custom domain
    (§8.16). A verify endpoint covers on-demand checks; this sweep catches the
    common "added the record later" case without the admin re-triggering."""

    async def _verify(session: AsyncSession) -> int:
        stmt = select(TenantDomain).where(
            TenantDomain.verification_status != DomainVerificationStatus.VERIFIED,
            TenantDomain.verification_token.is_not(None),
        )
        domains = list((await session.execute(stmt)).scalars())
        verified = 0
        for domain in domains:
            assert domain.verification_token is not None
            present = await txt_record_present(
                domain.domain, domain.verification_token, resolver=default_txt_lookup
            )
            if present:
                domain.verification_status = DomainVerificationStatus.VERIFIED
                domain.verified_at = datetime.now(UTC)
                verified += 1
        return verified

    return run_scoped(None, _verify)
