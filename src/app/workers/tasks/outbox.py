"""Transactional-outbox relay (§12) — the Beat job that drains the outbox.

Runs every minute, once per active tenant (RLS is fail-closed — the outbox is
tenant-scoped, so an unscoped drain would see nothing). For each tenant it claims
due pending events ``FOR UPDATE SKIP LOCKED`` and runs their registered handlers
inside the tenant's transaction; ``core.events.drain_outbox`` owns the
at-least-once + backoff logic, this task is just the scheduler shell (the same
"loop over active tenants via run_scoped_many" shape as the other sweeps).

**Handler registration.** Handlers are bound at import time from the modules that
own them (leads' speed-to-lead, webhooks' fan-out). A worker process must import
those modules or the registry is empty — so this task imports them explicitly
(the app process gets them for free via the router graph; a bare worker does
not).
"""

from collections.abc import Awaitable, Callable

import structlog
from celery import shared_task
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Import for their import-time ``register_handler`` side effects — a worker that
# runs only this task would otherwise have an empty handler registry.
import app.modules.leads.service
import app.modules.webhooks.service  # noqa: F401  # side-effect: register_handler
from app.core.events import drain_outbox
from app.core.tenancy import TenantContext
from app.modules.tenants.models import Tenant, TenantStatus
from app.workers.db import run_scoped_many

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


@shared_task(name="app.workers.tasks.outbox.relay_outbox")
def relay_outbox() -> dict[str, int]:
    """Drain due outbox events for every active tenant. Returns totals for the
    run (delivered/failed across all tenants) — handy in logs, idempotent."""

    async def _tenants(session: AsyncSession) -> list[Tenant]:
        # A suspended (e.g. non-paying) tenant's events are deliberately *not*
        # drained — their notifications/webhooks pause with the account. The rows
        # stay PENDING (not failed): ``attempts`` only advances on an actual
        # drain, so nothing exhausts while suspended, and reactivation resumes
        # delivery from where it paused. Same "skip suspended" stance as every
        # other Beat sweep (leads/appointments/blog).
        stmt = select(Tenant).where(Tenant.status != TenantStatus.SUSPENDED)
        return list((await session.execute(stmt)).scalars())

    tenants = run_scoped_many([(None, _tenants)])[0]

    def _drain_for(
        tenant: Tenant,
    ) -> Callable[[AsyncSession], Awaitable[tuple[int, int]]]:
        context = _to_context(tenant)

        async def _run(session: AsyncSession) -> tuple[int, int]:
            return await drain_outbox(session, context)

        return _run

    results = run_scoped_many([(t.id, _drain_for(t)) for t in tenants])
    delivered = sum(d for d, _ in results)
    failed = sum(f for _, f in results)
    if delivered or failed:
        logger.info("outbox_relay_done", delivered=delivered, failed=failed)
    return {"delivered": delivered, "failed": failed}


__all__ = ["relay_outbox"]
