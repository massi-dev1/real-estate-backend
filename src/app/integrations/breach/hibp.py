"""Have I Been Pwned breached-password check (§10.3, §5 ``integrations/``).

Infrastructure, not a feature module: no DB, no RBAC, no router. A password
chosen from a public breach corpus is the single most likely way an account
here is taken over, so registration and password changes reject known-breached
values outright.

**k-anonymity — the password never leaves the process.** We SHA-1 the password
locally, send only the first *five hex characters* of the digest, and get back
every suffix HIBP holds under that prefix (typically a few hundred). The match
is done here, against the local digest. HIBP therefore learns a 5-char prefix
shared by hundreds of thousands of passwords and nothing else. (SHA-1 is not a
security choice here — it is the corpus's index format. The password's *own*
storage is Argon2id, ``core.security``.)

**Fail-open, deliberately.** If HIBP is slow, down, or unreachable, the check
returns "not breached" and the password is accepted. Blocking every signup and
password reset in the product because a free third-party API is having an
outage would be a self-inflicted outage of our own, traded against a
probabilistic improvement in password quality. The check is a filter on bad
choices, not an authentication control. Set ``hibp_enabled=false`` to skip it
entirely (the offline default path for tests and air-gapped deploys).
"""

import hashlib

import httpx
import structlog

from app.core.config import Settings

logger = structlog.get_logger(__name__)


class BreachChecker:
    """Checks a password against the HIBP range API (k-anonymity)."""

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.hibp_enabled
        self._url = settings.hibp_api_url.rstrip("/")
        self._timeout = settings.hibp_timeout_seconds

    async def is_breached(self, password: str) -> bool:
        """True only when the password is *positively known* to be breached.

        Every failure mode — disabled, timeout, transport error, non-200,
        unparseable body — returns ``False`` (fail-open, see module docstring).
        """
        if not self._enabled:
            return False
        digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
        prefix, suffix = digest[:5], digest[5:]
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    f"{self._url}/{prefix}",
                    headers={"Add-Padding": "true"},
                )
            if response.status_code != httpx.codes.OK:
                logger.warning("hibp_unexpected_status", status=response.status_code)
                return False
            body = response.text
        except Exception:
            # Never surface the password or the digest in the log line.
            logger.warning("hibp_check_failed", prefix=prefix)
            return False

        for line in body.splitlines():
            candidate, _, count = line.strip().partition(":")
            if candidate != suffix:
                continue
            # `Add-Padding` makes HIBP return decoy rows with a count of 0 so
            # the response size leaks nothing; a real hit always has count > 0.
            try:
                return int(count) > 0
            except ValueError:
                return True
        return False


def build_breach_checker(settings: Settings) -> BreachChecker:
    """Build a checker for the given settings.

    Constructed fresh per caller rather than cached in a module global: the
    checker holds only config (no connection state — each ``is_breached`` call
    opens and closes its own short-lived httpx client), and a process-wide
    singleton would silently ignore a later caller built with different
    ``hibp_*`` settings and leak that config across a test process.
    """
    return BreachChecker(settings)


__all__ = ["BreachChecker", "build_breach_checker"]
