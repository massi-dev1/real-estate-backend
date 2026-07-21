"""Portal syndication task (§8.14) — runs on the dedicated ``sync`` queue.

``sync_listing_to_portal`` pushes/updates/removes one listing on one portal,
enqueued post-commit from listing lifecycle events (publish/update/archive) and
from the admin's manual re-push. It re-derives the tenant context (for
``settings.syndication``) inside a tenant-scoped transaction, runs the
:class:`SyndicationService` sync, and lets Celery's **built-in retry/backoff**
handle transient failures.

Retry policy mirrors the media pipeline's validation-vs-infrastructure split,
but the decision is made *inside* the service and signalled back on the
:class:`SyncOutcome`: a transient failure (``retry=True``) with the circuit still
closed makes this task ``self.retry`` with exponential backoff; a permanent
failure, a paused circuit, a success, or "nothing to do" all return without
retrying. The sync-state row records every outcome either way, so the admin UI
reflects reality regardless of retries.
"""

import uuid

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.tenancy import TenantContext
from app.integrations.portals.base import PortalAction
from app.modules.syndication.service import (
    MAX_SYNC_RETRIES,
    SyncOutcome,
    build_syndication_service_for_worker,
)
from app.modules.tenants.models import Tenant
from app.workers.db import run_scoped

logger = structlog.get_logger(__name__)


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value,
        settings=tenant.settings,
    )


@shared_task(
    bind=True,
    name="app.workers.tasks.syndication.sync_listing_to_portal",
    max_retries=MAX_SYNC_RETRIES,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def sync_listing_to_portal(
    self: object, tenant_id: str, listing_id: str, portal_key: str, action: str
) -> str:
    """One portal sync for one listing. Args are primitives so the task survives
    a broker restart (§12)."""
    tid = uuid.UUID(tenant_id)
    lid = uuid.UUID(listing_id)
    settings = get_settings()

    async def _run(session: AsyncSession) -> SyncOutcome:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tid))
        ).scalar_one_or_none()
        if tenant is None:
            return SyncOutcome(status="skipped", detail="tenant gone")
        service = build_syndication_service_for_worker(session, settings)
        return await service.sync_to_portal(
            _to_context(tenant), lid, portal_key, PortalAction(action)
        )

    # The tenant lookup itself needs the tenant GUC set (RLS on listings/media/
    # portal_sync_state reads happen inside the same scoped transaction).
    outcome = run_scoped(tid, _run)

    if outcome.retry:
        logger.info(
            "portal_sync_retry",
            portal_key=portal_key,
            listing_id=listing_id,
            detail=outcome.detail,
        )
        # The scoped transaction above already committed the FAILED state row, so
        # a retry (or its exhaustion) leaves an accurate record either way.
        raise self.retry(  # type: ignore[attr-defined]
            exc=RuntimeError(f"portal sync transient failure: {outcome.detail}")
        )

    logger.info(
        "portal_sync_done",
        portal_key=portal_key,
        listing_id=listing_id,
        status=outcome.status,
    )
    return outcome.status


__all__ = ["sync_listing_to_portal"]
