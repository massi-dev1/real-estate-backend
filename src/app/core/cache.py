"""Redis cache-aside for hot reads (§11).

The blueprint names a handful of reads that dominate anonymous traffic and
change rarely — ``GET /site/config``, public content pages/nav, search facet
counts, map clusters. This module caches them behind a small
:func:`cache_aside` helper so the loader (a DB round-trip, or a PostGIS
aggregate) runs only on a miss.

**Versioned keys, not TTL-only.** A key is
``cache:{tenant}:{entity}:{id}:v{N}`` where ``N`` is a per-``(tenant, entity)``
version counter. Invalidating on write is a single ``INCR`` of that counter
rather than a scan-and-delete: the next read computes a *new* key and misses,
and the stale entries simply age out under their own TTL. This means a write
never has to know every cached key it might have affected — bumping the entity
version retires all of them at once, which is exactly right for
"all pages changed when any page was published".

**Degrades open**, like the rate limiter and idempotency layers (§10.2): any
Redis failure (reading the version, the value, or writing either) falls back
to calling the loader directly. The cache is a latency optimisation, never a
correctness guarantee that must hold when Redis is down — and ``cache_enabled``
turns the whole layer off with the same fallthrough.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.metrics import record_cache_lookup

logger = structlog.get_logger(__name__)

# A value that never appears as a real version, so a missing counter is
# unambiguous. Redis ``INCR`` on a missing key starts at 1, so versions are
# always >= 1 and the first read (no counter yet) hashes to ``v0`` — its own
# key, which simply never collides with a post-write ``v1`` entry.
_INITIAL_VERSION = 0


def _version_key(tenant_id: str, entity: str) -> str:
    return f"cacheversion:{tenant_id}:{entity}"


def value_key(tenant_id: str, entity: str, ident: str, version: int) -> str:
    return f"cache:{tenant_id}:{entity}:{ident}:v{version}"


async def _current_version(redis: Redis, tenant_id: str, entity: str) -> int:
    raw = await redis.get(_version_key(tenant_id, entity))
    if raw is None:
        return _INITIAL_VERSION
    try:
        return int(raw)
    except (TypeError, ValueError):
        return _INITIAL_VERSION


async def bump_version(redis: Redis | None, tenant_id: str, entity: str) -> None:
    """Invalidate every cached entry for ``(tenant, entity)`` in O(1).

    Call this from the write path (create/update/publish/delete) of anything
    :func:`cache_aside` reads. Degrades open — a failed bump only means a read
    may serve a stale value until its TTL expires, never an error to the
    caller.
    """
    if redis is None:
        return
    try:
        await redis.incr(_version_key(tenant_id, entity))
    except Exception:
        logger.warning("cache_version_bump_failed", entity=entity)


async def cache_aside[T](
    redis: Redis | None,
    *,
    tenant_id: str,
    entity: str,
    ident: str,
    ttl_seconds: int,
    loader: Callable[[], Awaitable[T]],
    serialize: Callable[[T], Any] = lambda v: v,
    deserialize: Callable[[Any], T] = lambda v: v,
    enabled: bool = True,
) -> T:
    """Return a cached value or compute it via ``loader`` and cache the result.

    ``entity`` groups keys for versioned invalidation (e.g. ``"site_config"``,
    ``"content_page"``); ``ident`` distinguishes entries within an entity (a
    slug, a viewport hash, or ``"_"`` for a singleton). ``serialize`` /
    ``deserialize`` bridge the value and its JSON storage form — the default
    identity pair works for plain JSON-able values (dicts, lists, scalars).

    On any Redis error, or when disabled, the loader is called and its value
    returned uncached.
    """
    if not enabled or redis is None:
        return await loader()

    try:
        version = await _current_version(redis, tenant_id, entity)
        key = value_key(tenant_id, entity, ident, version)
        cached = await redis.get(key)
    except Exception:
        logger.warning("cache_read_failed", entity=entity)
        return await loader()

    if cached is not None:
        try:
            value = deserialize(json.loads(cached))
        except Exception:
            # A corrupt/incompatible blob (e.g. after a shape change) is a
            # miss, not a 500 — fall through and recompute.
            logger.warning("cache_deserialize_failed", entity=entity)
        else:
            record_cache_lookup(entity, hit=True)
            return value

    record_cache_lookup(entity, hit=False)
    value = await loader()
    try:
        await redis.set(key, json.dumps(serialize(value)), ex=ttl_seconds)
    except Exception:
        # Storing failed (Redis down, value not JSON-able) — the value is
        # still correct, just uncached this time.
        logger.warning("cache_write_failed", entity=entity)
    return value


__all__ = ["bump_version", "cache_aside", "value_key"]
