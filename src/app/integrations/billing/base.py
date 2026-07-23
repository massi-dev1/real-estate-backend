"""Billing provider seam (§8.16, §5 ``integrations/``).

Infrastructure, not a feature module: no DB, no RBAC, no router. Defines the
common contract every billing provider (Stripe primary, Chargily for DZ) is
driven through, plus the neutral event shape a provider webhook is normalised
into so the ``modules/tenants`` billing service never touches a provider SDK.

Same "design the seam, defer the live integration" stance as Part 19's
e-signature and Part 20's portal adapter: no real Stripe/Chargily credentials
exist in this environment, so the concrete adapter shipped is a **clearly
labelled sandbox stub** (:class:`StubBillingProvider`) — it signs and verifies
webhooks with a shared secret exactly as the real hardening rules (§10.9)
require, so the whole verification/idempotency path is real and tested, but it
never calls out to a live payment API.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


class BillingError(Exception):
    """A provider call failed. ``permanent`` splits an unrecoverable rejection
    (bad request/config) from a transient transport failure, mirroring the
    portal adapter's error split."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class WebhookVerificationError(Exception):
    """A webhook signature/freshness check failed (§10.9). Surfaced as 400 by
    the router — never processed."""


class UnhandledBillingEvent(Exception):
    """A validly-signed webhook of a type the app does not act on. Not an error
    — the caller acks it (200) without processing, per §10.9's "ignore unknown
    event types". ``event_id`` is still recorded for the idempotency log."""

    def __init__(self, event_id: str) -> None:
        super().__init__(f"unhandled billing event {event_id}")
        self.event_id = event_id


class BillingEventType(enum.StrEnum):
    """The provider-neutral subscription-lifecycle events the app reacts to.

    Each provider adapter maps its own event names onto these; the tenants
    billing service only ever switches on this set, so a new provider needs no
    changes past its adapter.
    """

    SUBSCRIPTION_ACTIVATED = "subscription.activated"
    SUBSCRIPTION_RENEWED = "subscription.renewed"
    PAYMENT_FAILED = "payment.failed"
    SUBSCRIPTION_CANCELED = "subscription.canceled"


@dataclass(frozen=True, slots=True)
class BillingEvent:
    """A normalised, verified provider webhook event.

    ``event_id`` is the provider's own id — the idempotency key (§10.9). The
    provider-specific fields the billing service needs (subscription id,
    customer id, plan, period end) are lifted into named attributes so the
    service never digs through raw provider JSON; ``raw`` keeps the original
    for the audit/idempotency-log payload.
    """

    provider: str
    event_id: str
    type: BillingEventType
    created_at: datetime
    subscription_id: str | None = None
    customer_id: str | None = None
    plan: str | None = None
    current_period_end: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CheckoutSession:
    """What a provider returns when a tenant starts a subscription — the URL the
    admin is redirected to, and the provider's session id for reconciliation."""

    url: str
    session_id: str


@runtime_checkable
class BillingProvider(Protocol):
    """The contract every billing provider satisfies (§8.16).

    ``key`` is the stable provider identifier persisted in
    ``tenant_subscriptions.provider`` and ``billing_events.provider``.
    """

    @property
    def key(self) -> str: ...

    async def create_checkout_session(
        self, *, tenant_id: str, plan: str, customer_email: str
    ) -> CheckoutSession:
        """Begin a subscription for ``plan`` — returns the hosted-checkout URL."""

    async def cancel_subscription(self, *, subscription_id: str) -> None:
        """Cancel a live subscription at the provider."""

    def verify_and_parse_webhook(
        self, *, payload: bytes, signature: str, now: datetime
    ) -> BillingEvent:
        """Verify the webhook signature + freshness (§10.9) and return the
        normalised event. Raises :class:`WebhookVerificationError` on a bad or
        stale signature — the caller turns that into a 400, never a 500, and
        never processes the body."""
