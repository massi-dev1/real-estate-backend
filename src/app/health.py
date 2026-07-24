"""Liveness and readiness endpoints (§14).

``/healthz`` — process is up (no dependencies touched).
``/readyz``  — Postgres and Redis reachable; load balancers gate on this.

Readiness also *reports* the Celery broker and object storage, but does **not**
gate on them (§14): they are diagnostic, not admission criteria.

* The **broker** is a distinct Redis database from the cache by default
  (``celery_broker_url`` db 2 vs ``redis_url`` db 0) and may be a distinct
  instance, so it can fail independently; it is reported, not gated, because a
  broker outage stalls background work while the API keeps answering.
* **Storage** is deliberately non-gating. Presigned upload/download URLs are
  computed locally and every object read/write happens in a Celery task, so an
  S3 outage degrades the media pipeline but leaves the entire API serving. Failing
  readiness would pull healthy replicas out of the load balancer over a
  dependency they do not need to answer a request — a self-inflicted outage.
"""

import asyncio

import structlog
from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.storage import ObjectStorage

# Bound *every* probe — gating and diagnostic alike — so a hung dependency can
# never hold a readiness request open past a load balancer's own timeout.
_PROBE_TIMEOUT_SECONDS = 2.0

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _ping_database(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))


async def _check_database(engine: AsyncEngine) -> bool:
    try:
        # Timed out like every other probe — and this one *gates* readiness, so
        # it matters most: a Postgres that accepts the TCP connection and then
        # stalls (pool exhaustion, saturated server, blackholed route) would
        # otherwise hang /readyz forever, leaking a connection per probe
        # interval and compounding the exhaustion that caused it.
        await asyncio.wait_for(_ping_database(engine), _PROBE_TIMEOUT_SECONDS)
    except Exception:
        logger.warning("readyz_database_unreachable")
        return False
    return True


async def _check_redis(redis: Redis) -> bool:
    try:
        return bool(await asyncio.wait_for(redis.ping(), _PROBE_TIMEOUT_SECONDS))
    except Exception:
        logger.warning("readyz_redis_unreachable")
        return False


async def _check_broker(broker: Redis) -> bool:
    """Ping the Celery broker over the long-lived client built in the lifespan.

    A distinct client from ``app.state.redis`` because the broker URL may point
    at a different instance/DB index — but built once at startup, not per probe:
    readiness is polled on a fixed interval, and a fresh pool per poll would
    churn connections against the broker precisely when it is already degraded.
    """
    try:
        return bool(await asyncio.wait_for(broker.ping(), _PROBE_TIMEOUT_SECONDS))
    except Exception:
        logger.warning("readyz_broker_unreachable")
        return False


async def _check_storage(storage: ObjectStorage) -> bool:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(storage.bucket_reachable), _PROBE_TIMEOUT_SECONDS
        )
    except Exception:
        logger.warning("readyz_storage_unreachable")
        return False


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    redis: Redis = request.app.state.redis
    db_ok = await _check_database(request.app.state.engine)
    redis_ok = await _check_redis(redis)
    broker_ok = await _check_broker(request.app.state.broker_redis)
    storage_ok = await _check_storage(request.app.state.storage)

    # Only Postgres and Redis gate readiness — the API cannot answer a single
    # request without them. Broker/storage are reported for operators.
    ready = db_ok and redis_ok
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if ready else "degraded",
        "database": "up" if db_ok else "down",
        "redis": "up" if redis_ok else "down",
        "broker": "up" if broker_ok else "down",
        "storage": "up" if storage_ok else "down",
    }
