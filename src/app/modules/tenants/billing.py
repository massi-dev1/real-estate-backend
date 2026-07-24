"""Billing lifecycle: checkout, webhook processing, dunning, trial expiry (§8.16).

The provider (Stripe/Chargily, or the sandbox stub) is reached only through the
``integrations/billing`` seam — this service never touches a provider SDK. It
owns the ``tenant_subscriptions`` mirror and drives tenant status via the
existing ``TenantService`` suspend/activate machinery (Part 2), never
reimplementing it.

Webhook handling follows the §10.9 hardening rules verbatim: the provider
adapter verifies the HMAC signature + ±5-minute freshness and normalises the
event; this service then makes it idempotent by ``(provider, event_id)`` before
applying any state change. An unknown-but-signed event is acked without effect.
"""

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.exceptions import NotFoundError
from app.integrations.billing.base import (
    BillingEvent,
    BillingEventType,
    BillingProvider,
    CheckoutSession,
    UnhandledBillingEvent,
)
from app.integrations.billing.registry import build_billing_provider
from app.modules.tenants.models import (
    SubscriptionStatus,
    TenantStatus,
    TenantSubscription,
)
from app.modules.tenants.plans import PLANS
from app.modules.tenants.repository import TenantRepository
from app.modules.tenants.service import TenantService, build_tenant_boundary

_KNOWN_PLANS = frozenset(PLANS)

logger = structlog.get_logger(__name__)


class BillingService:
    def __init__(
        self,
        repo: TenantRepository,
        tenants: TenantService,
        provider: BillingProvider,
        settings: Settings,
    ) -> None:
        self.repo = repo
        self.tenants = tenants
        self.provider = provider
        self.settings = settings

    # ---- checkout ----

    async def start_checkout(
        self, tenant_id: uuid.UUID, plan: str, customer_email: str
    ) -> CheckoutSession:
        tenant = await self.repo.get(tenant_id)
        if tenant is None:
            raise NotFoundError("Tenant not found.")
        return await self.provider.create_checkout_session(
            tenant_id=str(tenant_id), plan=plan, customer_email=customer_email
        )

    async def get_subscription(self, tenant_id: uuid.UUID) -> TenantSubscription | None:
        return await self.repo.get_subscription(tenant_id)

    # ---- webhook (§10.9) ----

    async def handle_webhook(self, *, payload: bytes, signature: str) -> tuple[bool, bool]:
        """Verify → idempotency-guard → dispatch. Returns
        ``(received, processed)``. Raises :class:`WebhookVerificationError` for a
        bad/stale signature (the router maps it to 400). A duplicate or unknown
        event is ``received=True, processed=False`` — acked, no effect."""
        try:
            event = self.provider.verify_and_parse_webhook(
                payload=payload, signature=signature, now=datetime.now(UTC)
            )
        except UnhandledBillingEvent as unhandled:
            # Signed but of a type we ignore — still log it for idempotency/audit.
            if unhandled.event_id:
                await self.repo.record_billing_event(
                    provider=self.provider.key,
                    event_id=unhandled.event_id,
                    event_type="unhandled",
                    payload={},
                )
            return True, False

        is_new = await self.repo.record_billing_event(
            provider=event.provider,
            event_id=event.event_id,
            event_type=event.type.value,
            payload=event.raw,
        )
        if not is_new:
            # Replay: already processed. Ack without re-applying (idempotent).
            logger.info("billing_event_duplicate", event_id=event.event_id)
            return True, False

        await self._apply(event)
        return True, True

    async def _apply(self, event: BillingEvent) -> None:
        subscription = await self._resolve_subscription(event)
        if subscription is None:
            logger.warning(
                "billing_event_no_subscription",
                event_id=event.event_id,
                type=event.type.value,
            )
            return

        if event.type in (
            BillingEventType.SUBSCRIPTION_ACTIVATED,
            BillingEventType.SUBSCRIPTION_RENEWED,
        ):
            subscription.status = SubscriptionStatus.ACTIVE
            subscription.grace_until = None
            if event.current_period_end is not None:
                subscription.current_period_end = event.current_period_end
            if event.plan and event.plan in _KNOWN_PLANS:
                subscription.plan = event.plan
                await self.tenants.set_plan(subscription.tenant_id, event.plan)
            elif event.plan:
                # The provider named a plan we don't model. Don't poison the
                # event (it was already recorded as processed — raising here
                # would 409 the webhook and the provider's retry would be
                # swallowed as a duplicate): keep the mirror's own plan and
                # log it for follow-up, but still activate the subscription.
                logger.warning(
                    "billing_event_unknown_plan",
                    event_id=event.event_id,
                    plan=event.plan,
                    tenant_id=str(subscription.tenant_id),
                )
            # A paid subscription reactivates a suspended/trial tenant, and
            # clears any in-flight offboard (a renewal must not leave the tenant
            # scheduled for purge — same effect as cancel_offboard).
            await self._reactivate_tenant(subscription.tenant_id)
        elif event.type is BillingEventType.PAYMENT_FAILED:
            subscription.status = SubscriptionStatus.PAST_DUE
            # Open a dunning grace window; the sweep suspends past it.
            subscription.grace_until = datetime.now(UTC) + timedelta(
                days=self.settings.billing_grace_days
            )
        elif event.type is BillingEventType.SUBSCRIPTION_CANCELED:
            subscription.status = SubscriptionStatus.CANCELED
            # A cancellation suspends the tenant immediately (access ends).
            await self.tenants.set_status(subscription.tenant_id, TenantStatus.SUSPENDED)
        await self.repo.flush()

    async def _resolve_subscription(self, event: BillingEvent) -> TenantSubscription | None:
        """Find the subscription this event targets, creating the mirror row on
        first activation (the provider is source of truth; the first webhook
        seeds our copy). ``customer_id`` carrying our tenant id lets the very
        first activation attach to the right tenant."""
        if event.subscription_id:
            existing = await self.repo.get_subscription_by_provider_id(
                event.provider, event.subscription_id
            )
            if existing is not None:
                return existing
        # First activation: seed the mirror. The stub/real provider is expected
        # to carry our tenant id in customer_id (metadata) on the checkout it
        # created; without it we cannot attach the subscription.
        tenant_id = self._tenant_id_from_event(event)
        if tenant_id is None:
            return None
        tenant = await self.repo.get(tenant_id)
        if tenant is None:
            return None
        existing = await self.repo.get_subscription(tenant_id)
        if existing is not None:
            # Attach the provider ids to the tenant's existing mirror row.
            existing.provider_subscription_id = event.subscription_id
            existing.provider_customer_id = event.customer_id
            return existing
        subscription = TenantSubscription(
            tenant_id=tenant_id,
            provider=event.provider,
            plan=event.plan or tenant.plan,
            status=SubscriptionStatus.INCOMPLETE,
            provider_customer_id=event.customer_id,
            provider_subscription_id=event.subscription_id,
        )
        self.repo.add_subscription(subscription)
        await self.repo.flush()
        return subscription

    @staticmethod
    def _tenant_id_from_event(event: BillingEvent) -> uuid.UUID | None:
        for candidate in (event.customer_id, (event.raw.get("data") or {}).get("tenant_id")):
            if candidate:
                try:
                    return uuid.UUID(str(candidate))
                except (ValueError, TypeError):
                    continue
        return None

    async def _reactivate_tenant(self, tenant_id: uuid.UUID) -> None:
        """Bring a paid tenant back to ACTIVE, and cancel any in-flight offboard
        so the scheduled-purge sweep can't delete a tenant that just paid."""
        tenant = await self.repo.get(tenant_id)
        if tenant is None:
            return
        if tenant.deletion_scheduled_at is not None or tenant.offboarding_at is not None:
            # Undo the offboard schedule (mirrors TenantService.cancel_offboard).
            tenant.offboarding_at = None
            tenant.deletion_scheduled_at = None
        if tenant.status is not TenantStatus.ACTIVE:
            await self.tenants.set_status(tenant_id, TenantStatus.ACTIVE)
        else:
            await self.repo.flush()

    # ---- dunning + trial sweeps (called from Beat tasks) ----

    async def suspend_grace_expired(self) -> int:
        """Suspend tenants whose past-due grace window has closed (§8.16). Reuses
        the existing suspend machinery — never reimplements it."""
        now = datetime.now(UTC)
        subs = await self.repo.list_grace_expired_subscriptions(now=now)
        for sub in subs:
            await self.tenants.set_status(sub.tenant_id, TenantStatus.SUSPENDED)
            # Clear the window so a re-run does not re-suspend endlessly.
            sub.grace_until = None
        await self.repo.flush()
        return len(subs)

    async def expire_trials(self) -> int:
        """Suspend trial tenants whose trial has ended without an active
        subscription (§8.16)."""
        now = datetime.now(UTC)
        tenants = await self.repo.list_trials_expiring(now=now)
        suspended = 0
        for tenant in tenants:
            sub = await self.repo.get_subscription(tenant.id)
            if sub is not None and sub.status is SubscriptionStatus.ACTIVE:
                # Paid before the trial lapsed — activate instead of suspend.
                await self.tenants.set_status(tenant.id, TenantStatus.ACTIVE)
                continue
            await self.tenants.set_status(tenant.id, TenantStatus.SUSPENDED)
            suspended += 1
        return suspended


def build_billing_service(session: AsyncSession, redis: Redis) -> BillingService:
    settings = get_settings()
    return BillingService(
        TenantRepository(session),
        build_tenant_boundary(session, redis),
        build_billing_provider(settings),
        settings,
    )
