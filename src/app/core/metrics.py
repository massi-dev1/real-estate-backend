"""Prometheus metrics (§14): HTTP RED metrics, infrastructure gauges, and the
business counters the blueprint names (leads/hour, notification-delivery rate).

Design notes:

* **Counters, not rates.** ``leads_created_total`` and
  ``notification_sends_total`` are monotonic counters; "leads per hour" and
  "delivery rate" are ``rate()``/ratio queries in Prometheus. The app never
  tracks a time window itself — that is the scraper's job, and a counter
  survives restarts/replicas in a way an app-side rate cannot.
* **Gauges are sampled at scrape time**, not on a background timer: the
  collector callbacks in :func:`collect_runtime_metrics` run when ``/metrics``
  is scraped, so a dependency being slow costs a scrape, never a request.
* **Always importable, cheap when disabled.** Instrumentation calls are plain
  counter increments with no I/O; ``metrics_enabled=False`` only unmounts the
  endpoint. Nothing here needs credentials (offline-safe, §14).

The default registry is used so ``prometheus_client``'s own process/GC
collectors come along for free.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

import structlog
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from redis.asyncio import Redis
from starlette.types import ASGIApp, Message, Receive, Scope, Send

if TYPE_CHECKING:  # pragma: no cover - typing only
    from sqlalchemy.ext.asyncio import AsyncEngine

# Bound every scrape-time probe: a reachable-but-slow dependency must cost one
# scrape, never accumulate pending scrapes on the shared event loop.
_PROBE_TIMEOUT_SECONDS = 2.0

logger = structlog.get_logger(__name__)

# ---- HTTP (RED: rate, errors, duration) ----

REQUESTS = Counter(
    "http_requests_total",
    "HTTP requests by route template, method and status.",
    ["method", "route", "status"],
)
REQUEST_DURATION = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency by route template and method.",
    ["method", "route"],
    # Tuned for an API whose §11 budget is ~200ms: fine grain under 1s,
    # coarse above it (anything past 1s is already a problem, not a percentile).
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# ---- business metrics (§14) ----

LEADS_CREATED = Counter(
    "leads_created_total",
    "Leads created, by capture source. `rate(...[1h])` is the §14 leads/hour.",
    ["source"],
)
NOTIFICATION_SENDS = Counter(
    "notification_sends_total",
    "Notification delivery attempts by channel and outcome. The §14 delivery "
    "rate is sent / (sent + failed).",
    ["channel", "status"],
)

# ---- infrastructure gauges (sampled at scrape time) ----

DB_POOL_SIZE = Gauge("db_pool_connections", "SQLAlchemy pool connections by state.", ["state"])
CELERY_QUEUE_DEPTH = Gauge("celery_queue_depth", "Pending tasks per Celery queue.", ["queue"])
CACHE_HIT_RATIO = Gauge(
    "cache_hit_ratio",
    "Redis keyspace hit ratio (hits / (hits + misses)) since the server started.",
)

# Queues declared in ``workers/celery_app.py``; a Redis broker stores each as a
# plain list keyed by queue name, so LLEN is the pending depth.
CELERY_QUEUES: tuple[str, ...] = ("default", "media", "sync", "analytics")


def record_lead_created(source: str) -> None:
    """Called from the leads service on every created lead (§14 leads/hour)."""
    LEADS_CREATED.labels(source=source).inc()


def record_notification_send(channel: str, status: str) -> None:
    """Called from the notification delivery task on every attempt (§14)."""
    NOTIFICATION_SENDS.labels(channel=channel, status=status).inc()


def _route_template(scope: Scope) -> str:
    """The matched route's *full* path template (``/api/v1/listings/{ref_or_id}``).

    Labelling by the raw path would create one time series per listing id — an
    unbounded-cardinality explosion that kills a Prometheus server. Unmatched
    paths (including a tenant-middleware short-circuit, which never reaches the
    router) collapse to a single ``__unmatched__`` series for the same reason.

    FastAPI mounts routers as nested sub-routers, so ``scope["route"].path`` is
    only the *mount-relative* template (``/listings/{ref_or_id}``) — two
    different mounts sharing a sub-path would collide into one series. The full
    template is recovered by substituting the matched path params back into the
    concrete request path, which is prefix-agnostic and needs no knowledge of
    how the routers were nested.
    """
    route: Any = scope.get("route")
    if route is None:
        return "__unmatched__"

    path: object = scope.get("path")
    params: object = scope.get("path_params")
    if not isinstance(path, str):
        return "__unmatched__"
    if not isinstance(params, dict) or not params:
        return path

    # Substitute right-to-left: a param's value can coincide with a literal
    # segment earlier in the path (``/api/v1/.../v1``), and path params always
    # sit at the tail end of the concrete path.
    template = path
    for name, value in params.items():
        text = str(value)
        # A `{x:path}` convertor's regex is `.*`, which matches the empty
        # string; `rpartition("")` raises ValueError, and this runs in the
        # middleware's `finally`, so it would turn a good response into a 500.
        if not text:
            continue
        head, sep, tail = template.rpartition(text)
        if sep:
            template = f"{head}{{{name}}}{tail}"
    return template


class MetricsMiddleware:
    """Records request count + latency. Pure ASGI (matching the other
    middleware); the route template is only known *after* the router has run,
    so it is read from the scope in the ``finally`` block."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            route = _route_template(scope)
            method = scope["method"]
            REQUEST_DURATION.labels(method=method, route=route).observe(time.perf_counter() - start)
            REQUESTS.labels(method=method, route=route, status=str(status_code)).inc()


async def _collect_queue_depths(broker: Redis) -> None:
    """Sample per-queue backlog from the **broker** client.

    It must be the broker's client, not ``app.state.redis``: the cache and the
    broker are different Redis databases by default (``redis_url`` is db 0,
    ``celery_broker_url`` db 2), so sampling the cache client would LLEN keys
    that only ever exist on the broker and report 0 for every queue — silent
    exactly during the backlog the §14 alert exists to catch.
    """
    for queue in CELERY_QUEUES:
        try:
            # redis-py types llen as sync-or-awaitable on the shared command
            # mixin; on the asyncio client it is always awaitable.
            depth: int = await asyncio.wait_for(
                broker.llen(queue),  # type: ignore[arg-type]
                _PROBE_TIMEOUT_SECONDS,
            )
            CELERY_QUEUE_DEPTH.labels(queue=queue).set(depth)
        except Exception:
            # `continue`, not `break`: one unreadable queue must not blind the
            # remaining ones (they would silently keep a stale value forever).
            logger.warning("metrics_queue_depth_unavailable", queue=queue)
            continue


async def collect_runtime_metrics(
    engine: AsyncEngine | None, redis: Redis | None, broker: Redis | None = None
) -> None:
    """Refresh the sampled gauges. Best-effort throughout: a scrape must never
    fail (or hang the scraper) because a dependency is degraded — the gauge
    simply keeps its previous value and the failure is logged. Every probe is
    timeout-bounded so a reachable-but-slow dependency cannot let concurrent
    scrapes pile up on the event loop that also serves real traffic."""
    if engine is not None:
        try:
            pool: Any = engine.pool
            DB_POOL_SIZE.labels(state="in_use").set(pool.checkedout())
            DB_POOL_SIZE.labels(state="idle").set(pool.checkedin())
            DB_POOL_SIZE.labels(state="overflow").set(pool.overflow())
        except Exception:  # pragma: no cover - defensive
            logger.warning("metrics_db_pool_unavailable")

    if redis is not None:
        try:
            info = await asyncio.wait_for(redis.info("stats"), _PROBE_TIMEOUT_SECONDS)
            hits = float(info.get("keyspace_hits", 0))
            misses = float(info.get("keyspace_misses", 0))
            total = hits + misses
            CACHE_HIT_RATIO.set(hits / total if total else 0.0)
        except Exception:
            logger.warning("metrics_cache_stats_unavailable")

    if broker is not None:
        await _collect_queue_depths(broker)


def render_metrics() -> tuple[bytes, str]:
    """The Prometheus exposition payload and its content type."""
    return generate_latest(), CONTENT_TYPE_LATEST
