"""Redis sliding-window rate limiting (§10.8).

Applied to the public lead-capture endpoint in this part; the factory is
deliberately generic (tenant + IP scoped, pluggable key/limit/window) so
auth endpoints can adopt it later without rework — not retrofitted now.
"""

import time
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Request

from app.core.exceptions import RateLimitedError
from app.core.tenancy import TenantDep

logger = structlog.get_logger(__name__)


def rate_limit(
    *, key_prefix: str, limit: int, window_seconds: int
) -> Callable[..., Awaitable[None]]:
    """Dependency factory: a sliding-window log (sorted set) keyed on
    tenant + client IP, so two agencies never share a spam budget and a
    single caller cannot burst past the limit at a window boundary the way a
    fixed-window counter would allow.
    """

    async def _check(request: Request, tenant: TenantDep) -> None:
        redis = request.app.state.redis
        ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{key_prefix}:{tenant.id}:{ip}"
        now = time.time()
        try:
            pipe = redis.pipeline()
            pipe.zremrangebyscore(key, 0, now - window_seconds)
            pipe.zadd(key, {str(uuid.uuid4()): now})
            pipe.zcard(key)
            pipe.expire(key, window_seconds)
            results = await pipe.execute()
            count = results[2]
        except Exception:
            # Degrade-open, consistent with the jti-denylist check's stance
            # (permissions.py::get_current_user) — Redis being down must not
            # take capture endpoints down with it.
            logger.warning("rate_limit_check_failed", key_prefix=key_prefix)
            return
        if count > limit:
            raise RateLimitedError("Too many requests. Please try again shortly.")

    return _check
