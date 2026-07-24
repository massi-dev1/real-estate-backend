"""Sandbox billing provider (§8.16) — a clearly-labelled stub.

No live Stripe/Chargily credentials exist in this environment, so this stands
in for the real thing. Crucially, its **webhook verification is real**: it uses
the same Stripe-style signed-header scheme (`t=<unix>,v1=<hmac-sha256>`) and
the same ±5-minute freshness window the §10.9 hardening rules mandate, so the
tenants billing service exercises the genuine verify → idempotency → dispatch
path. Only the outbound calls (checkout/cancel) are faked. Swapping in a real
Stripe adapter is a matter of reimplementing this protocol against the SDK; no
call site in ``modules/tenants`` changes.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.integrations.billing.base import (
    BillingEvent,
    BillingEventType,
    CheckoutSession,
    UnhandledBillingEvent,
    WebhookVerificationError,
)

PROVIDER_KEY = "stub"
# Webhook freshness window (§10.9): a signature older than this is rejected even
# if valid, blunting replay.
WEBHOOK_TOLERANCE = timedelta(minutes=5)

# The provider's own event names → our neutral set. A real adapter maps the
# provider's actual names here (e.g. Stripe's "invoice.payment_failed").
_EVENT_TYPE_MAP: dict[str, BillingEventType] = {
    "subscription.activated": BillingEventType.SUBSCRIPTION_ACTIVATED,
    "subscription.renewed": BillingEventType.SUBSCRIPTION_RENEWED,
    "payment.failed": BillingEventType.PAYMENT_FAILED,
    "subscription.canceled": BillingEventType.SUBSCRIPTION_CANCELED,
}


class StubBillingProvider:
    """A :class:`~app.integrations.billing.base.BillingProvider` implementation
    with real webhook verification and faked outbound calls."""

    def __init__(self, webhook_secret: str) -> None:
        self._secret = webhook_secret.encode()

    @property
    def key(self) -> str:
        return PROVIDER_KEY

    async def create_checkout_session(
        self, *, tenant_id: str, plan: str, customer_email: str
    ) -> CheckoutSession:
        session_id = f"cs_stub_{uuid.uuid4().hex}"
        # A real provider returns its hosted-checkout URL here.
        return CheckoutSession(
            url=f"https://billing.stub.local/checkout/{session_id}?plan={plan}",
            session_id=session_id,
        )

    async def cancel_subscription(self, *, subscription_id: str) -> None:
        # No-op in the sandbox: the real cancellation happens provider-side and
        # arrives back as a subscription.canceled webhook.
        return None

    # ---- webhook signing (test/dev helper) + verification (real) ----

    def sign_payload(self, payload: bytes, *, timestamp: datetime | None = None) -> str:
        """Produce a `t=<unix>,v1=<hmac>` header for ``payload``. Used by the
        test suite (and any local webhook simulator) to forge a *validly
        signed* event; a real provider signs on its side."""
        ts = int((timestamp or datetime.now(UTC)).timestamp())
        signature = self._hmac(ts, payload)
        return f"t={ts},v1={signature}"

    def _hmac(self, timestamp: int, payload: bytes) -> str:
        signed = f"{timestamp}.".encode() + payload
        return hmac.new(self._secret, signed, hashlib.sha256).hexdigest()

    def verify_and_parse_webhook(
        self, *, payload: bytes, signature: str, now: datetime
    ) -> BillingEvent:
        timestamp, provided = self._parse_header(signature)
        expected = self._hmac(timestamp, payload)
        if not hmac.compare_digest(provided, expected):
            raise WebhookVerificationError("Signature verification failed.")
        event_time = datetime.fromtimestamp(timestamp, tz=UTC)
        if abs(now - event_time) > WEBHOOK_TOLERANCE:
            raise WebhookVerificationError("Webhook timestamp is outside the tolerance window.")
        return self._parse_event(payload, event_time)

    @staticmethod
    def _parse_header(signature: str) -> tuple[int, str]:
        parts = dict(item.split("=", 1) for item in signature.split(",") if "=" in item)
        try:
            return int(parts["t"]), parts["v1"]
        except (KeyError, ValueError) as exc:
            raise WebhookVerificationError("Malformed signature header.") from exc

    def _parse_event(self, payload: bytes, event_time: datetime) -> BillingEvent:
        try:
            body: dict[str, Any] = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise WebhookVerificationError("Webhook payload is not valid JSON.") from exc
        raw_type = body.get("type")
        event_type = _EVENT_TYPE_MAP.get(str(raw_type))
        event_id = body.get("id")
        if event_type is None or not event_id:
            # An unknown event type is not an error — a real provider sends many
            # events we ignore. Signal it with a sentinel the caller skips.
            raise UnhandledBillingEvent(str(event_id or ""))
        data: dict[str, Any] = body.get("data") or {}
        period_end = data.get("current_period_end")
        return BillingEvent(
            provider=PROVIDER_KEY,
            event_id=str(event_id),
            type=event_type,
            created_at=event_time,
            subscription_id=data.get("subscription_id"),
            customer_id=data.get("customer_id"),
            plan=data.get("plan"),
            current_period_end=(
                datetime.fromtimestamp(period_end, tz=UTC)
                if isinstance(period_end, int | float)
                else None
            ),
            raw=body,
        )
