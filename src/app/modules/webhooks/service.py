"""Outbound webhook business logic (§8.14, §10.9).

Three concerns:

1. **Admin CRUD** (request-time, portal router): register/edit/delete endpoints,
   view the delivery log. The signing ``secret`` is minted here and revealed
   exactly once (on create).
2. **Event fan-out** (``dispatch_event``): the outbox handler for a domain event
   — for each active, non-tripped endpoint subscribed to the event, create a
   ``pending`` delivery row and enqueue its delivery task. Registered with
   ``core.events`` at import time so the outbox relay routes events here.
3. **Delivery execution** (``deliver``, worker-time): sign the payload
   (HMAC-SHA256, Stripe-style ``t=,v1=`` header, §10.9), POST it through the
   **SSRF-guarded** client (§10.4 — the target is re-resolved and re-checked on
   every hop, so a rebind/redirect into a private range can't sneak through),
   record the outcome, and drive the **circuit breaker** (§8.14, same pattern as
   ``modules/syndication``): N consecutive failures open the breaker and stop
   delivery until an admin re-enables the endpoint. Whether Celery should retry
   is signalled back on :class:`DeliveryOutcome`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any

import httpx
import structlog
from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.database import SessionDep, on_commit
from app.core.events import (
    EVENT_DEAL_CLOSED,
    EVENT_LEAD_CREATED,
    EVENT_LISTING_PUBLISHED,
    register_handler,
)
from app.core.exceptions import NotFoundError
from app.core.net import SsrfError, build_guarded_client, validate_public_url
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.tenancy import TenantContext
from app.modules.webhooks.models import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from app.modules.webhooks.repository import WebhookRepository
from app.modules.webhooks.schemas import WebhookEndpointCreate, WebhookEndpointUpdate

logger = structlog.get_logger(__name__)

# Consecutive failures before the breaker opens (mirrors syndication).
CIRCUIT_BREAKER_THRESHOLD = 5
# Retry ceiling handed to Celery for a transient delivery failure.
MAX_DELIVERY_RETRIES = 6
# A receiver has this long to respond before the attempt is a transport failure.
DELIVERY_TIMEOUT_SECONDS = 10.0
SIGNATURE_HEADER = "X-Webhook-Signature"
EVENT_HEADER = "X-Webhook-Event"


def sign_webhook(secret: str, payload: bytes, *, timestamp: datetime | None = None) -> str:
    """Produce the ``t=<unix>,v1=<hmac-sha256>`` signature header for ``payload``
    (§10.9, same scheme as the inbound billing webhook so the codebase has one
    signing convention). The receiver recomputes the HMAC over
    ``f"{t}.".encode() + payload`` with the shared secret to verify."""
    ts = int((timestamp or datetime.now(UTC)).timestamp())
    signed = f"{ts}.".encode() + payload
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return f"t={ts},v1={digest}"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """What the delivery task should do next. ``retry`` is True only for a
    transient failure while the circuit is still closed — the task raises to let
    Celery back off. A 2xx, a permanent 4xx, a paused circuit, or a vanished row
    all return ``retry=False``."""

    status: str
    retry: bool = False
    detail: str = ""


class WebhookService:
    def __init__(self, repo: WebhookRepository, settings: Settings) -> None:
        self.repo = repo
        self.settings = settings

    # ---- admin CRUD ----

    async def create_endpoint(
        self, tenant: TenantContext, data: WebhookEndpointCreate
    ) -> WebhookEndpoint:
        """Register an endpoint. The URL is SSRF-validated up front (§10.4) so a
        misconfigured internal target fails loudly as a 422 rather than silently
        never delivering. The signing secret is minted here."""
        self._validate_url(data.url)
        endpoint = WebhookEndpoint(
            tenant_id=tenant.id,
            url=data.url,
            secret=secrets.token_urlsafe(32),
            events=data.events,
            description=data.description,
        )
        self.repo.add(endpoint)
        await self.repo.flush()
        return endpoint

    async def list_endpoints(self, tenant: TenantContext) -> list[WebhookEndpoint]:
        return await self.repo.list_endpoints(tenant.id)

    async def get_endpoint(self, tenant: TenantContext, endpoint_id: uuid.UUID) -> WebhookEndpoint:
        endpoint = await self.repo.get_endpoint(tenant.id, endpoint_id)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found.")
        return endpoint

    async def update_endpoint(
        self, tenant: TenantContext, endpoint_id: uuid.UUID, data: WebhookEndpointUpdate
    ) -> WebhookEndpoint:
        endpoint = await self.repo.get_endpoint(tenant.id, endpoint_id, for_update=True)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found.")
        fields = data.model_dump(exclude_unset=True)
        if "url" in fields and fields["url"] is not None:
            self._validate_url(fields["url"])
            endpoint.url = fields["url"]
        if "events" in fields and fields["events"] is not None:
            endpoint.events = fields["events"]
        if "description" in fields:
            endpoint.description = fields["description"]
        if "is_active" in fields and fields["is_active"] is not None:
            endpoint.is_active = fields["is_active"]
            # Re-activating a manually-disabled endpoint also clears a tripped
            # breaker — the admin's explicit "turn it back on" resets the count,
            # the same recovery path as syndication's manual re-push.
            if fields["is_active"]:
                endpoint.circuit_open = False
                endpoint.consecutive_failures = 0
        await self.repo.flush()
        return endpoint

    async def delete_endpoint(self, tenant: TenantContext, endpoint_id: uuid.UUID) -> None:
        endpoint = await self.repo.get_endpoint(tenant.id, endpoint_id)
        if endpoint is None:
            raise NotFoundError("Webhook endpoint not found.")
        await self.repo.delete_endpoint(endpoint)

    # ---- delivery log ----

    async def list_deliveries(
        self,
        tenant: TenantContext,
        *,
        endpoint_id: uuid.UUID | None,
        cursor: str | None,
        limit: int | None,
    ) -> tuple[list[WebhookDelivery], str | None]:
        page_size = clamp_limit(limit)
        after = _decode_cursor(cursor) if cursor else None
        rows = await self.repo.list_deliveries(
            tenant.id, endpoint_id=endpoint_id, after=after, limit=page_size
        )
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        return items, next_cursor

    # ---- event fan-out (outbox handler) ----

    async def dispatch_event(
        self, tenant: TenantContext, event_type: str, payload: dict[str, Any]
    ) -> int:
        """Fan a domain event out to every subscribed endpoint: one ``pending``
        delivery row per endpoint, each enqueued post-commit. Returns the number
        of deliveries created. This is the outbox handler — it runs inside the
        relay's transaction, so the delivery rows commit with the outbox row
        being marked delivered (at-least-once: a relay crash re-runs this, which
        would create duplicate delivery rows, but the *receiver* dedupes on the
        signed event — acceptable and documented)."""
        endpoints = await self.repo.endpoints_for_event(tenant.id, event_type)
        for endpoint in endpoints:
            delivery = WebhookDelivery(
                tenant_id=tenant.id,
                endpoint_id=endpoint.id,
                event_type=event_type,
                payload=payload,
            )
            self.repo.add(delivery)
            await self.repo.flush()
            _enqueue_delivery(self.repo.session, tenant.id, delivery.id)
        return len(endpoints)

    # ---- delivery execution (worker) ----

    async def deliver(self, tenant: TenantContext, delivery_id: uuid.UUID) -> DeliveryOutcome:
        """POST one signed delivery to its endpoint and record the outcome.
        Called by the delivery Celery task inside a tenant-scoped transaction."""
        delivery = await self.repo.get_delivery(tenant.id, delivery_id, for_update=True)
        if delivery is None:
            return DeliveryOutcome(status="skipped", detail="delivery gone")
        if delivery.status is DeliveryStatus.DELIVERED:
            return DeliveryOutcome(status="delivered", detail="already delivered")

        endpoint = await self.repo.get_endpoint(tenant.id, delivery.endpoint_id, for_update=True)
        if endpoint is None:
            delivery.status = DeliveryStatus.FAILED
            delivery.last_error = "endpoint deleted"
            return DeliveryOutcome(status="failed", detail="endpoint gone")
        if endpoint.circuit_open:
            # The breaker tripped after this delivery was enqueued. Park it
            # FAILED rather than leaving it dangling ``pending`` forever (the
            # delivery task is one-shot, not a sweep — nothing would re-pick it
            # up). New events after the admin re-enables the endpoint create
            # fresh deliveries; this one is not silently retried into the storm.
            delivery.status = DeliveryStatus.FAILED
            delivery.last_error = "circuit open"
            return DeliveryOutcome(status="paused", detail="circuit open")

        delivery.attempts += 1
        body = json.dumps(
            {"event": delivery.event_type, "data": delivery.payload}, separators=(",", ":")
        ).encode()
        signature = sign_webhook(endpoint.secret, body)
        try:
            status_code = await self._post(endpoint.url, body, delivery.event_type, signature)
        except SsrfError as exc:
            # The URL was public at registration but now resolves internal (or a
            # redirect pointed inward) — a permanent, security-relevant failure.
            return self._record_failure(
                delivery, endpoint, str(exc), permanent=True, response_status=None
            )
        except httpx.HTTPError as exc:
            return self._record_failure(
                delivery, endpoint, f"transport error: {exc}", permanent=False, response_status=None
            )

        if 200 <= status_code < 300:
            return self._record_success(delivery, endpoint, status_code)
        # 4xx (except 408/429) is the receiver rejecting the payload — permanent;
        # 5xx / 408 / 429 are transient (retry with backoff).
        permanent = 400 <= status_code < 500 and status_code not in (408, 429)
        return self._record_failure(
            delivery,
            endpoint,
            f"HTTP {status_code}",
            permanent=permanent,
            response_status=status_code,
        )

    async def _post(self, url: str, body: bytes, event_type: str, signature: str) -> int:
        async with build_guarded_client(
            allow_private_hosts=self.settings.webhook_allow_private_hosts,
            timeout=DELIVERY_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                url,
                content=body,
                headers={
                    "Content-Type": "application/json",
                    SIGNATURE_HEADER: signature,
                    EVENT_HEADER: event_type,
                },
            )
        return response.status_code

    def _record_success(
        self, delivery: WebhookDelivery, endpoint: WebhookEndpoint, status_code: int
    ) -> DeliveryOutcome:
        now = datetime.now(UTC)
        delivery.status = DeliveryStatus.DELIVERED
        delivery.response_status = status_code
        delivery.delivered_at = now
        delivery.last_error = None
        endpoint.consecutive_failures = 0
        endpoint.circuit_open = False
        endpoint.last_error = None
        endpoint.last_delivered_at = now
        return DeliveryOutcome(status="delivered", detail=f"HTTP {status_code}")

    def _record_failure(
        self,
        delivery: WebhookDelivery,
        endpoint: WebhookEndpoint,
        detail: str,
        *,
        permanent: bool,
        response_status: int | None,
    ) -> DeliveryOutcome:
        delivery.response_status = response_status
        delivery.last_error = detail[:2000]
        endpoint.consecutive_failures += 1
        endpoint.last_error = detail[:2000]
        if endpoint.consecutive_failures >= CIRCUIT_BREAKER_THRESHOLD:
            endpoint.circuit_open = True
            delivery.status = DeliveryStatus.FAILED
            logger.warning(
                "webhook_circuit_opened",
                endpoint_id=str(endpoint.id),
                failures=endpoint.consecutive_failures,
            )
            return DeliveryOutcome(status="paused", detail=detail)
        if permanent:
            delivery.status = DeliveryStatus.FAILED
            return DeliveryOutcome(status="failed", retry=False, detail=detail)
        # Transient: leave the delivery pending for the retry.
        return DeliveryOutcome(status="failed", retry=True, detail=detail)

    def _validate_url(self, url: str) -> None:
        try:
            validate_public_url(url, allow_private_hosts=self.settings.webhook_allow_private_hosts)
        except SsrfError as exc:
            # Surfaced by the router as a 422 (a bad target the admin can fix).
            raise WebhookUrlError(str(exc)) from exc


class WebhookUrlError(Exception):
    """A webhook target URL failed SSRF validation at registration (§10.4).
    The router maps it to a 422 — a client-fixable bad request."""


def _decode_cursor(cursor: str) -> tuple[str, uuid.UUID]:
    values = decode_cursor(cursor)
    try:
        return values["created_at"], uuid.UUID(values["id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc


def _enqueue_delivery(session: AsyncSession, tenant_id: uuid.UUID, delivery_id: uuid.UUID) -> None:
    """Register a post-commit enqueue of the delivery task (lazy import — the
    task module imports this service). Primitive args survive a broker restart."""

    async def _enqueue() -> None:
        from app.workers.tasks.webhooks import deliver_webhook

        deliver_webhook.delay(str(tenant_id), str(delivery_id))

    on_commit(session, _enqueue)


# ---- outbox handler registration (code-owned wiring, §12) ----


async def _dispatch_via_outbox(
    session: AsyncSession, tenant: TenantContext, event_type: str, payload: dict[str, Any]
) -> None:
    """Outbox handler for the webhook-carrying domain events — one function
    serves every subscribable event (the event name arrives as ``event_type``)."""
    service = WebhookService(WebhookRepository(session), get_settings())
    await service.dispatch_event(tenant, event_type, payload)


for _event in (EVENT_LEAD_CREATED, EVENT_LISTING_PUBLISHED, EVENT_DEAL_CLOSED):
    register_handler(_event, _dispatch_via_outbox)


def build_webhook_service_for_worker(session: AsyncSession) -> WebhookService:
    """Worker-side construction (no ``request``)."""
    return WebhookService(WebhookRepository(session), get_settings())


def get_webhook_service(session: SessionDep, request: Request) -> WebhookService:
    return WebhookService(WebhookRepository(session), request.app.state.settings)


WebhookServiceDep = Annotated[WebhookService, Depends(get_webhook_service)]
