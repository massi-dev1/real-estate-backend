"""Transactional outbox (§12) — reliable domain-event fan-out.

The problem the outbox solves: a side effect enqueued in a **post-commit hook**
(``on_commit``) is not part of the row's transaction. The row commits, then the
enqueue runs — and a broker hiccup, a worker crash, or a lost network packet in
that gap drops the side effect silently. For a low-criticality effect (a cache
bust, a nice-to-have digest) that is an acceptable trade. For a *lead's*
speed-to-lead notification it is not: the lead is in the database, the agency is
paying for it, and no one was told.

The outbox makes the event durable by writing an ``outbox`` row **in the same
transaction** as the triggering change. Either both commit or neither does —
there is no window. A Beat-driven relay (``workers.tasks.outbox``) then drains
pending rows and dispatches each to its handler with **at-least-once** delivery:

- A row is claimed with ``SELECT ... FOR UPDATE SKIP LOCKED`` so two relay ticks
  never process the same row.
- On success it is marked ``delivered`` (a terminal, idempotent state — a
  re-drain skips it).
- On failure it is left ``pending`` with an incremented ``attempts`` and an
  exponentially-backed-off ``next_attempt_at``; past ``OUTBOX_MAX_ATTEMPTS`` it
  moves to ``failed`` (a poison message never blocks the queue).

At-least-once means a handler **must be idempotent** — the same event may be
dispatched twice (relay claimed a row, delivered, then crashed before the commit
marking it delivered). Every registered handler here already is (``notify()``
writes one in-app row keyed by content; webhook delivery is keyed by delivery id).

Handlers are registered in code (``register_handler``), not the DB — the set of
domain events is auditable in git like the RBAC matrix, and a payload is a plain
JSON dict so the row survives a deploy that changes a handler's internals.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.core.tenancy import TenantContext

logger = structlog.get_logger(__name__)

# Domain event names. Constants (not free strings) so a producer and the handler
# registry can never silently disagree on spelling.
EVENT_LEAD_CREATED = "lead.created"
EVENT_LISTING_PUBLISHED = "listing.published"
EVENT_DEAL_CLOSED = "deal.closed"

# Relay policy. A transient handler failure is retried with exponential backoff
# up to a ceiling; a row that exhausts its attempts is parked ``failed`` so one
# poison event cannot wedge the drain.
OUTBOX_MAX_ATTEMPTS = 8
OUTBOX_BACKOFF_BASE_SECONDS = 30
OUTBOX_BACKOFF_MAX_SECONDS = 3600
# How many due rows one relay tick claims per tenant — bounds a tick's work.
OUTBOX_BATCH_SIZE = 100


class OutboxStatus(enum.StrEnum):
    PENDING = "pending"  # not yet delivered (may be mid-backoff)
    DELIVERED = "delivered"  # a handler acknowledged it — terminal
    FAILED = "failed"  # exhausted its attempts — terminal, needs a human


class OutboxEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One durable domain event, written in the producer's transaction (§12).

    Tenant-RLS (``tenant_id``); the relay runs tenant-scoped like every other
    sweep so RLS stays the safety net even from a worker."""

    __tablename__ = "outbox"
    __table_args__ = (
        # The relay's hot query: due pending rows for a tenant, oldest first.
        Index(
            "ix_outbox_due",
            "tenant_id",
            "status",
            "next_attempt_at",
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]]
    status: Mapped[OutboxStatus] = mapped_column(
        Enum(
            OutboxStatus,
            name="outbox_status",
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=OutboxStatus.PENDING,
        server_default=OutboxStatus.PENDING.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    # Due time for the next delivery attempt; NOW() on insert so a fresh row is
    # immediately eligible. Backoff pushes it into the future after a failure.
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()")
    )
    last_error: Mapped[str | None] = mapped_column(String(500))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# ---- producer API (called inside the triggering transaction) ----


def emit_event(
    session: AsyncSession,
    tenant: TenantContext,
    event_type: str,
    payload: dict[str, Any],
) -> OutboxEvent:
    """Stage a domain event on the current transaction.

    This is a plain ``session.add`` — **no post-commit hook**. That is the whole
    point: the event row commits atomically with the change that produced it, so
    the event can never be lost in the gap between commit and enqueue. The relay
    picks it up on its next tick. Returns the (unflushed) row for the rare caller
    that wants its id; most callers ignore it."""
    event = OutboxEvent(tenant_id=tenant.id, event_type=event_type, payload=payload)
    session.add(event)
    return event


# ---- handler registry (code-owned, like the RBAC matrix) ----

# A handler consumes one delivered event. It runs inside the relay's
# tenant-scoped transaction and may itself register post-commit side effects
# (e.g. notify()'s WS push) — the relay drains those exactly as a request does.
# Handlers receive the event name so one function can serve several event types.
EventHandler = Callable[[AsyncSession, TenantContext, str, dict[str, Any]], Awaitable[None]]

# Several independent handlers may consume one event (e.g. ``lead.created`` fans
# out to *both* the speed-to-lead notification and the outbound-webhook
# dispatch). They run in registration order; one failing rolls back the whole
# event's transaction, so the relay re-attempts the event and every handler is
# re-run — which is why handlers must be idempotent (at-least-once, §12).
_HANDLERS: dict[str, list[EventHandler]] = {}


def register_handler(event_type: str, handler: EventHandler) -> None:
    """Add a handler for an event name. Called at import time from the module
    that owns the side effect (keeps ``core`` free of module imports — the wiring
    lives in the feature module, ``core.events`` only holds the registry)."""
    _HANDLERS.setdefault(event_type, []).append(handler)


def get_handlers(event_type: str) -> list[EventHandler]:
    return _HANDLERS.get(event_type, [])


# ---- relay (called by the Beat task, per tenant, inside a scoped txn) ----


def _backoff_seconds(attempts: int) -> int:
    """Exponential backoff for the ``attempts``-th failure, capped."""
    delay: int = OUTBOX_BACKOFF_BASE_SECONDS * (2 ** max(attempts - 1, 0))
    return min(delay, OUTBOX_BACKOFF_MAX_SECONDS)


async def drain_outbox(session: AsyncSession, tenant: TenantContext) -> tuple[int, int]:
    """Deliver every due pending event for one tenant. Returns
    ``(delivered, failed)`` counts. Called inside a tenant-scoped transaction —
    the whole batch commits (or rolls back) with the session boundary.

    Rows are claimed ``FOR UPDATE SKIP LOCKED`` so overlapping ticks never
    double-claim; a handler with no registration is parked ``failed`` (a producer
    emitted an event nothing consumes — surface it, don't loop forever).

    Each event's handlers run inside a **savepoint** (``begin_nested``): a handler
    failure rolls back only that event's side effects (so a half-applied event is
    never committed), while the outer transaction stays usable to record the
    retry/fail bookkeeping and to process the *rest* of the batch — one poison
    event must not abort the whole tick."""
    now = datetime.now(UTC)
    stmt = (
        select(OutboxEvent)
        .where(
            OutboxEvent.tenant_id == tenant.id,
            OutboxEvent.status == OutboxStatus.PENDING,
            OutboxEvent.next_attempt_at <= now,
        )
        .order_by(OutboxEvent.next_attempt_at, OutboxEvent.id)
        .limit(OUTBOX_BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    rows = list((await session.execute(stmt)).scalars())

    delivered = 0
    failed = 0
    for event in rows:
        handlers = get_handlers(event.event_type)
        # Compute the new attempt count as a local — the row-mutating bookkeeping
        # runs *after* the savepoint resolves so it is never captured (and rolled
        # back) by a handler failure's savepoint.
        attempt_no = event.attempts + 1
        if not handlers:
            event.attempts = attempt_no
            event.status = OutboxStatus.FAILED
            event.last_error = "no handler registered"
            failed += 1
            logger.warning("outbox_no_handler", event_type=event.event_type, id=str(event.id))
            continue
        try:
            async with session.begin_nested():
                for handler in handlers:
                    await handler(session, tenant, event.event_type, event.payload)
        except Exception as exc:
            # The savepoint has rolled back the handlers' partial work; the outer
            # transaction is intact, so record the failure and move on.
            event.attempts = attempt_no
            event.last_error = str(exc)[:500]
            if attempt_no >= OUTBOX_MAX_ATTEMPTS:
                event.status = OutboxStatus.FAILED
                failed += 1
                logger.error(
                    "outbox_exhausted",
                    event_type=event.event_type,
                    id=str(event.id),
                    attempts=attempt_no,
                )
            else:
                event.next_attempt_at = now + timedelta(seconds=_backoff_seconds(attempt_no))
                logger.info(
                    "outbox_retry",
                    event_type=event.event_type,
                    id=str(event.id),
                    attempts=attempt_no,
                )
            continue
        event.attempts = attempt_no
        event.status = OutboxStatus.DELIVERED
        event.dispatched_at = now
        event.last_error = None
        delivered += 1

    return delivered, failed


__all__ = [
    "EVENT_DEAL_CLOSED",
    "EVENT_LEAD_CREATED",
    "EVENT_LISTING_PUBLISHED",
    "OUTBOX_MAX_ATTEMPTS",
    "EventHandler",
    "OutboxEvent",
    "OutboxStatus",
    "drain_outbox",
    "emit_event",
    "get_handlers",
    "register_handler",
]
