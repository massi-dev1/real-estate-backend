"""Liveness and readiness endpoints (§14).

``/healthz`` — process is up (no dependencies touched).
``/readyz``  — Postgres and Redis reachable; load balancers gate on this.
"""

import structlog
from fastapi import APIRouter, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


async def _check_database(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        logger.warning("readyz_database_unreachable")
        return False
    return True


async def _check_redis(redis: Redis) -> bool:
    try:
        return bool(await redis.ping())
    except Exception:
        logger.warning("readyz_redis_unreachable")
        return False


@router.get("/readyz")
async def readyz(request: Request, response: Response) -> dict[str, str]:
    redis: Redis = request.app.state.redis
    db_ok = await _check_database(request.app.state.engine)
    redis_ok = await _check_redis(redis)
    if not (db_ok and redis_ok):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ok" if db_ok and redis_ok else "degraded",
        "database": "up" if db_ok else "down",
        "redis": "up" if redis_ok else "down",
    }
