"""Outbound webhook delivery task (§8.14, §10.9) — runs on the ``sync`` queue.

``deliver_webhook`` POSTs one signed delivery to its endpoint, enqueued
post-commit from :meth:`WebhookService.dispatch_event` (itself the outbox handler
for a domain event). It re-derives the tenant context inside a tenant-scoped
transaction, runs the delivery, and lets Celery's **built-in retry/backoff**
handle a transient failure — the retry *decision* is made inside the service and
signalled back on the :class:`DeliveryOutcome`, exactly like portal syndication.

A permanent rejection (4xx), an open circuit, or a vanished row all return
without retrying; a 2xx marks the delivery delivered. Every outcome is recorded
on ``webhook_deliveries`` regardless, so the delivery log reflects reality.
"""

import uuid

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenancy import TenantContext
from app.modules.tenants.models import Tenant
from app.modules.webhooks.service import (
    MAX_DELIVERY_RETRIES,
    DeliveryOutcome,
    build_webhook_service_for_worker,
)
from app.workers.db import run_scoped

logger = structlog.get_logger(__name__)


def _to_context(tenant: Tenant) -> TenantContext:
    return TenantContext(
        id=tenant.id,
        slug=tenant.slug,
        name=tenant.name,
        status=tenant.status.value,
        settings=tenant.settings,
        plan=tenant.plan,
    )


@shared_task(
    bind=True,
    name="app.workers.tasks.webhooks.deliver_webhook",
    max_retries=MAX_DELIVERY_RETRIES,
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def deliver_webhook(self: object, tenant_id: str, delivery_id: str) -> str:
    """Deliver one signed webhook. Args are primitives so the task survives a
    broker restart (§12)."""
    tid = uuid.UUID(tenant_id)
    did = uuid.UUID(delivery_id)

    async def _run(session: AsyncSession) -> DeliveryOutcome:
        tenant = (
            await session.execute(select(Tenant).where(Tenant.id == tid))
        ).scalar_one_or_none()
        if tenant is None:
            return DeliveryOutcome(status="skipped", detail="tenant gone")
        service = build_webhook_service_for_worker(session)
        return await service.deliver(_to_context(tenant), did)

    outcome = run_scoped(tid, _run)

    if outcome.retry:
        logger.info("webhook_delivery_retry", delivery_id=delivery_id, detail=outcome.detail)
        # The scoped transaction already committed the incremented-attempt row,
        # so a retry (or its exhaustion) leaves an accurate record either way.
        raise self.retry(  # type: ignore[attr-defined]
            exc=RuntimeError(f"webhook delivery transient failure: {outcome.detail}")
        )

    logger.info("webhook_delivery_done", delivery_id=delivery_id, status=outcome.status)
    return outcome.status


__all__ = ["deliver_webhook"]
