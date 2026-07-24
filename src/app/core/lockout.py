"""Account lockout with exponential backoff (§7.1, §10.3).

The layer the rate limiter deliberately is *not*. ``core.rate_limit`` blunts
volume and **degrades open** when Redis is unavailable (§10.2, an
availability-over-enforcement trade for a limiter that guards no single
account); this guards one specific account against a credential-stuffing run
that stays comfortably inside any per-minute budget — five tries a minute from
a rotating IP pool will never trip a rate limit, but it will walk a password
list.

Two independent counters per attempt:

* ``auth:lockout:user:{tenant}:{email}`` — the account under attack, so a
  distributed attempt from many IPs is still stopped.
* ``auth:lockout:ip:{ip}`` — the source, so one host cannot spray many
  accounts, each of which alone stays under its own threshold.

Either being locked refuses the attempt. Backoff doubles per failure past the
threshold (``base * 2 ** (failures - threshold)``, capped), so a wrong password
typed twice costs nothing while a script is quickly pushed into hour-long
waits.

**Fail-open on a Redis error, like every other Redis touch point here** — but
the consequence is different and worth stating: an outage removes the lockout,
leaving only the password itself (Argon2id) and the rate limiter between an
attacker and an account. That is the same trade the codebase makes everywhere
(a cache outage must not lock every user out of a working system), and it is
why lockout is one layer of several rather than the only one.

The caller must keep the *response* identical whether an account is locked,
unknown, or simply given a wrong password (§7.1 no-enumeration) — this module
returns a boolean and never a reason a client could see.
"""

import hashlib
import time

import structlog
from redis.asyncio import Redis

from app.core.config import Settings

logger = structlog.get_logger(__name__)

_USER_KEY = "auth:lockout:user:{}:{}"
_IP_KEY = "auth:lockout:ip:{}"
_LOCK_SUFFIX = ":locked"


def _account_key(tenant_id: str, email: str) -> str:
    """Hash the email into the key: Redis keys surface in ``MONITOR``,
    ``SLOWLOG`` and RDB dumps, and the set of accounts under attack is itself
    worth not leaking (same stance as the idempotency actor key, §9)."""
    digest = hashlib.sha256(email.lower().encode()).hexdigest()[:32]
    return _USER_KEY.format(tenant_id, digest)


def _backoff_seconds(failures: int, settings: Settings) -> int:
    """Doubling backoff past the threshold, capped.

    ``failures`` at exactly the threshold gives the base delay; each further
    failure doubles it. The cap keeps a long-running attack from locking a real
    user out for days over an attacker's persistence.
    """
    over = max(0, failures - settings.login_max_failed_attempts)
    delay: int = settings.login_lockout_base_seconds * (2**over)
    return min(delay, settings.login_lockout_max_seconds)


class LoginLockout:
    """Per-account and per-IP failed-login tracking for one request."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings

    def _keys(self, tenant_id: str, email: str, ip: str) -> tuple[str, str]:
        return _account_key(tenant_id, email), _IP_KEY.format(ip)

    async def is_locked(self, tenant_id: str, email: str, ip: str) -> bool:
        """True when either the account or the source is inside a backoff
        window. Fails **open** on a Redis error (documented in the module
        docstring) — an outage must not lock everyone out of a working app."""
        account_key, ip_key = self._keys(tenant_id, email, ip)
        try:
            locks = await self.redis.mget([account_key + _LOCK_SUFFIX, ip_key + _LOCK_SUFFIX])
        except Exception:
            logger.warning("login_lockout_check_failed")
            return False
        return any(value is not None for value in locks)

    async def retry_after(self, tenant_id: str, email: str, ip: str) -> int:
        """Seconds until the *longer* of the two locks expires (best effort)."""
        account_key, ip_key = self._keys(tenant_id, email, ip)
        try:
            pipe = self.redis.pipeline()
            pipe.ttl(account_key + _LOCK_SUFFIX)
            pipe.ttl(ip_key + _LOCK_SUFFIX)
            ttls = await pipe.execute()
        except Exception:
            return self.settings.login_lockout_base_seconds
        return max([int(t) for t in ttls if isinstance(t, int) and t > 0], default=1)

    async def record_failure(self, tenant_id: str, email: str, ip: str) -> None:
        """Count a failed attempt against both keys, locking whichever crosses
        the threshold. Fail-soft: a Redis error only loses this one count."""
        account_key, ip_key = self._keys(tenant_id, email, ip)
        try:
            pipe = self.redis.pipeline()
            for key in (account_key, ip_key):
                pipe.incr(key)
                pipe.expire(key, self.settings.login_failure_window_seconds)
            results = await pipe.execute()
        except Exception:
            logger.warning("login_lockout_record_failed")
            return

        # incr/expire alternate, so the counts are at even indices.
        counts = {account_key: int(results[0]), ip_key: int(results[2])}
        for key, failures in counts.items():
            if failures < self.settings.login_max_failed_attempts:
                continue
            delay = _backoff_seconds(failures, self.settings)
            try:
                await self.redis.set(key + _LOCK_SUFFIX, str(int(time.time())), ex=delay)
            except Exception:
                logger.warning("login_lockout_set_failed")

    async def reset(self, tenant_id: str, email: str, ip: str) -> None:
        """Clear both counters after a genuine success — a person who mistypes
        twice a week must never accumulate their way into a lockout."""
        account_key, ip_key = self._keys(tenant_id, email, ip)
        try:
            await self.redis.delete(
                account_key, account_key + _LOCK_SUFFIX, ip_key, ip_key + _LOCK_SUFFIX
            )
        except Exception:
            logger.warning("login_lockout_reset_failed")


__all__ = ["LoginLockout"]
