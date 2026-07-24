"""``Idempotency-Key`` header facility for money/duplicate-sensitive POSTs (§9).

A client that retries a POST after a timeout (or double-taps a submit button)
should get the *same* result back, not a second lead/booking/checkout. The
client supplies an ``Idempotency-Key`` header; the first request executes
normally and its response is cached in Redis keyed on
``tenant + user (or "anon") + key + route``, so the same key on a different
route or by a different caller never collides. A replay within the TTL
returns the cached response byte-for-byte without re-running the handler; a
*concurrent* duplicate (the first attempt hasn't finished yet) gets a 409
rather than racing the handler twice.

Wired via :class:`IdempotentRoute`, a small ``APIRoute`` subclass applied to
individual routes (``router = APIRouter(route_class=IdempotentRoute)`` or
per-route via ``@router.post(..., route_class=...)`` is not supported by
FastAPI, so it is applied at the router level) — not global middleware,
because only a few money/duplicate-sensitive POSTs need this, and most
endpoints (GETs, idempotent-by-nature PUTs) must never cache a response
behind a client-supplied header they didn't ask to send.

Degrades open like the rate limiter: a Redis outage must not take these
endpoints down, so a lock/cache failure just lets the request execute
normally (§9 is a convenience against accidental duplicates, not a
correctness guarantee that must hold when the cache itself is unavailable).
"""

import json
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import Request, Response
from fastapi.routing import APIRoute
from redis.asyncio import Redis

from app.core.exceptions import IdempotencyConflictError

logger = structlog.get_logger(__name__)

_LOCK_SECONDS = 30  # generous upper bound on how long one of these POSTs can take


def _cache_key(*, tenant_id: str, actor_id: str, key: str, route: str) -> str:
    return f"idempotency:{tenant_id}:{actor_id}:{route}:{key}"


def _actor_id(request: Request) -> str:
    """The header-carried bearer identity, or ``"anon"`` for the public
    capture surfaces (lead capture, tour booking) this facility also guards —
    those have no ``AuthenticatedUser`` at all."""
    header = request.headers.get("authorization", "")
    if header:
        return header  # opaque; never decoded here, just used as a cache-key component
    return "anon"


def _tenant_id(request: Request) -> str:
    tenant = getattr(request.state, "tenant", None)
    return str(tenant.id) if tenant is not None else "platform"


class IdempotentRoute(APIRoute):
    """An ``APIRoute`` that caches/replays responses by ``Idempotency-Key``.

    Subclassing ``APIRoute`` (rather than a dependency) is what makes caching
    the *actual response* possible — a dependency runs before the handler and
    never sees what it returned, while the route handler wraps the whole
    request→response cycle.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            key = request.headers.get("idempotency-key")
            if not key:
                return await original(request)

            redis: Redis = request.app.state.redis
            settings = request.app.state.settings
            cache_key = _cache_key(
                tenant_id=_tenant_id(request),
                actor_id=_actor_id(request),
                key=key,
                route=self.path,
            )

            try:
                cached = await redis.get(cache_key)
            except Exception:
                logger.warning("idempotency_check_failed", route=self.path)
                return await original(request)

            if cached is not None:
                if cached == "__in_progress__":
                    raise IdempotencyConflictError(
                        "A request with this Idempotency-Key is already being processed."
                    )
                stored = json.loads(cached)
                return Response(
                    content=stored["body"],
                    status_code=stored["status_code"],
                    media_type=stored["media_type"],
                )

            try:
                acquired = await redis.set(cache_key, "__in_progress__", nx=True, ex=_LOCK_SECONDS)
            except Exception:
                logger.warning("idempotency_lock_failed", route=self.path)
                return await original(request)

            if not acquired:
                raise IdempotencyConflictError(
                    "A request with this Idempotency-Key is already being processed."
                )

            try:
                response = await original(request)
            except Exception:
                try:
                    await redis.delete(cache_key)
                except Exception:
                    logger.warning("idempotency_unlock_failed", route=self.path)
                raise

            if response.status_code < 500:
                try:
                    await redis.set(
                        cache_key,
                        json.dumps(
                            {
                                "status_code": response.status_code,
                                "media_type": response.media_type,
                                "body": bytes(response.body).decode(),
                            }
                        ),
                        ex=settings.idempotency_key_ttl_seconds,
                    )
                except Exception:
                    logger.warning("idempotency_store_failed", route=self.path)
            else:
                # A 5xx is not a result worth replaying — free the key so a
                # retry with the same Idempotency-Key executes fresh.
                try:
                    await redis.delete(cache_key)
                except Exception:
                    logger.warning("idempotency_unlock_failed", route=self.path)
            return response

        return handler
