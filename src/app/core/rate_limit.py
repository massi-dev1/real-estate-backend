"""Redis sliding-window rate limiting (§10.2, §10.8).

Two layers, both sharing one sliding-window-log implementation:

* :class:`GlobalRateLimitMiddleware` — a coarse per-IP budget in front of the
  whole app, so a single source cannot flood endpoints that carry no limit of
  their own.
* :func:`rate_limit` — a dependency factory for tight per-endpoint budgets
  (public capture surfaces, and the auth endpoints wired in Part 28).

Both **degrade open**: Redis being unavailable must not take the API down with
it. That is a deliberate availability-over-enforcement trade — the limiter
exists to blunt abuse, not to be the only thing standing between a caller and
an account, which is what the auth module's own lockout (Part 29) is for.

Both key on :func:`client_ip`, which honours ``X-Forwarded-For`` **only** when
the socket peer is inside the configured ``trusted_proxy_cidrs``. Behind the
§16 Caddy topology every peer is the proxy, so without that boundary the whole
internet would share a single budget; trusting the header without it would let
any caller mint a fresh identity per request. Neither failure mode is
acceptable, so the trust boundary is explicit config.
"""

import ipaddress
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import lru_cache

import structlog
from fastapi import Request
from redis.asyncio import Redis
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.config import Settings, get_settings
from app.core.exceptions import RateLimitedError, problem_response
from app.core.tenancy import TenantDep

logger = structlog.get_logger(__name__)

type ip_network_t = ipaddress.IPv4Network | ipaddress.IPv6Network

# Paths the global limiter never counts: infra probes and the metrics scrape
# are polled on a fixed interval by the platform itself, so budgeting them
# would mean a busy load balancer eventually rate-limits its own health check.
GLOBAL_LIMIT_EXEMPT_PREFIXES: tuple[str, ...] = ("/healthz", "/readyz", "/internal")


async def consume(redis: Redis, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
    """Record a hit and report ``(within_limit, retry_after_seconds)``.

    A sliding-window *log* (a sorted set of hit timestamps) rather than a
    fixed-window counter: a counter lets a caller spend a full budget at the
    end of one window and another immediately at the start of the next,
    passing twice the limit in a moment straddling the boundary.

    Raises nothing — a Redis failure propagates to the caller, which decides
    how to degrade.
    """
    now = time.time()
    pipe = redis.pipeline()
    pipe.zremrangebyscore(key, 0, now - window_seconds)
    pipe.zadd(key, {str(uuid.uuid4()): now})
    pipe.zcard(key)
    pipe.expire(key, window_seconds)
    results = await pipe.execute()
    count = int(results[2])
    if count <= limit:
        return True, 0
    # The budget frees up when the oldest hit in the window ages out.
    oldest = await redis.zrange(key, 0, 0, withscores=True)
    retry_after = window_seconds
    if oldest:
        retry_after = max(1, int(window_seconds - (now - oldest[0][1])) + 1)
    return False, retry_after


@lru_cache(maxsize=8)
def _trusted_networks(cidrs: str) -> tuple[ip_network_t, ...]:
    """Parse the configured trust boundary once per distinct config string.

    A bare address is accepted and normalised to a single-host network, so
    ``10.0.0.5`` and ``10.0.0.5/32`` behave identically. An unparseable entry is
    dropped with a warning rather than raising: a typo in one CIDR must not stop
    the app booting, and dropping it *narrows* trust (fails closed).
    """
    networks: list[ip_network_t] = []
    for raw in cidrs.split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("trusted_proxy_cidr_invalid", entry=entry)
    return tuple(networks)


def _is_trusted_proxy(peer: str, networks: tuple[ip_network_t, ...]) -> bool:
    if not networks:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    # An IPv4-mapped IPv6 peer (``::ffff:10.0.0.5``) must compare against the
    # v4 CIDR an operator actually wrote, the same unwrapping core.net does.
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in networks)


def _forwarded_client(header: str, hops: int) -> str | None:
    """The client address from ``X-Forwarded-For``, counted from the right.

    ``X-Forwarded-For`` is append-only: each proxy adds the peer it saw. Only
    the rightmost ``hops`` entries were written by infrastructure we trust, so
    the client is the entry just left of them. Counting from the *right* is what
    makes this unforgeable — a caller can prepend as many fake entries as it
    likes, but cannot push its own past the ones our trusted hops appended.
    """
    parts = [p.strip() for p in header.split(",") if p.strip()]
    index = len(parts) - hops
    if index < 0 or index >= len(parts):
        return None
    candidate = parts[index]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        # A malformed or obfuscated entry ("unknown", "_hidden" — both legal
        # per RFC 7239) is unusable as a budget key; fall back to the peer.
        return None
    return candidate


def client_ip(scope_or_request: Scope | Request, settings: Settings | None = None) -> str:
    """The address a rate-limit budget is keyed on.

    Defaults to the raw socket peer. ``X-Forwarded-For`` is honoured **only**
    when that peer is itself inside ``trusted_proxy_cidrs`` — trusting the
    header unconditionally lets any caller forge a fresh identity per request
    and erase its own limit, while ignoring it behind a proxy pools every
    client into the proxy's single budget (which is the failure this resolves).

    ``settings`` is optional so callers on a hot path can pass the instance they
    already hold; otherwise the process-wide cached settings are used.
    """
    if isinstance(scope_or_request, Request):
        peer = scope_or_request.client.host if scope_or_request.client else "unknown"
        headers = scope_or_request.headers
        forwarded = headers.get("x-forwarded-for")
    else:
        client = scope_or_request.get("client")
        peer = client[0] if client else "unknown"
        forwarded = None
        for name, value in scope_or_request.get("headers", ()):
            if name == b"x-forwarded-for":
                forwarded = value.decode("latin-1")
                break

    if not forwarded:
        return peer

    resolved = settings if settings is not None else get_settings()
    if not _is_trusted_proxy(peer, _trusted_networks(resolved.trusted_proxy_cidrs)):
        return peer
    return _forwarded_client(forwarded, resolved.trusted_proxy_hops) or peer


def rate_limit(
    *, key_prefix: str, limit: int, window_seconds: int
) -> Callable[..., Awaitable[None]]:
    """Dependency factory: a sliding-window log keyed on tenant + client IP, so
    two agencies never share a spam budget and a single caller cannot burst
    past the limit at a window boundary.
    """

    async def _check(request: Request, tenant: TenantDep) -> None:
        redis = request.app.state.redis
        caller = client_ip(request, request.app.state.settings)
        key = f"ratelimit:{key_prefix}:{tenant.id}:{caller}"
        try:
            ok, retry_after = await consume(redis, key, limit=limit, window_seconds=window_seconds)
        except Exception:
            # Degrade-open, consistent with the jti-denylist check's stance
            # (permissions.py::get_current_user) — Redis being down must not
            # take capture endpoints down with it.
            logger.warning("rate_limit_check_failed", key_prefix=key_prefix)
            return
        if not ok:
            raise RateLimitedError(
                "Too many requests. Please try again shortly.",
                retry_after=retry_after,
            )

    return _check


def auth_rate_limit(
    action: str, limit: int, window_seconds: int = 60
) -> Callable[..., Awaitable[None]]:
    """Per-endpoint limit for the credential-handling routes (§10.2).

    Keyed per *action* so exhausting the login budget does not also lock the
    caller out of a password reset, and per tenant+IP like every other limit
    here. Deliberately **not** keyed on the submitted email: an attacker
    controls that field, so it is free to rotate, and keying on it would let
    them lock a victim's account out of its own reset flow.
    """
    return rate_limit(key_prefix=f"auth:{action}", limit=limit, window_seconds=window_seconds)


class GlobalRateLimitMiddleware:
    """A coarse per-IP budget across the whole app (§10.2).

    Runs as raw ASGI, outside routing and tenant resolution, so a flood aimed
    at an unknown host or a nonexistent path is still counted — those cost a
    Redis lookup each, which is exactly what this layer is meant to bound.
    Per-endpoint limits sit *inside* this and are far tighter; this one only
    has to stop a single source from monopolising the process.
    """

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self._limit = settings.global_rate_limit_per_minute
        self._settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"].startswith(GLOBAL_LIMIT_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        redis: Redis | None = getattr(scope["app"].state, "redis", None)
        if redis is None:
            await self.app(scope, receive, send)
            return

        caller = client_ip(scope, self._settings)
        key = f"ratelimit:global:{caller}"
        try:
            ok, retry_after = await consume(redis, key, limit=self._limit, window_seconds=60)
        except Exception:
            logger.warning("global_rate_limit_check_failed")
            await self.app(scope, receive, send)
            return

        if ok:
            await self.app(scope, receive, send)
            return

        logger.info("global_rate_limited", ip=caller)
        response = problem_response(
            Request(scope),
            status_code=RateLimitedError.status_code,
            slug=RateLimitedError.slug,
            title=RateLimitedError.title,
            detail="Too many requests from this address. Please try again shortly.",
            headers={"Retry-After": str(retry_after)},
        )
        await response(scope, receive, send)


__all__ = [
    "GLOBAL_LIMIT_EXEMPT_PREFIXES",
    "GlobalRateLimitMiddleware",
    "auth_rate_limit",
    "client_ip",
    "consume",
    "rate_limit",
]
