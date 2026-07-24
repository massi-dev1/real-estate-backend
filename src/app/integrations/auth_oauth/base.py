"""OAuth provider contract (§7.1, §5 ``integrations/``).

Infrastructure, not a feature module: no DB, no RBAC, no router. The auth
module drives every social provider through this interface, so adding Facebook
or Apple later is a new adapter plus a registry entry — no call-site change.

Same "design the seam, defer the live integration" stance as the AI, billing,
portal and e-signature seams: no OAuth client is registered for this
deployment, so :func:`~app.integrations.auth_oauth.registry.build_oauth_provider`
returns ``None`` without credentials and the routes report "not configured"
rather than half-working.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class OAuthError(Exception):
    """An OAuth exchange failed. ``permanent`` splits an unrecoverable
    rejection (bad code, revoked client, mismatched redirect) from a transient
    transport failure, mirroring the other integration seams' error split."""

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class OAuthProfile:
    """The provider-neutral identity an authorization code resolves to.

    ``subject`` is the provider's stable id for the person — never the email,
    which people change and which a provider may not have verified.
    """

    subject: str
    email: str | None
    email_verified: bool
    first_name: str | None = None
    last_name: str | None = None


@runtime_checkable
class OAuthProvider(Protocol):
    """The contract every social-login provider satisfies."""

    @property
    def key(self) -> str: ...

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        """The provider URL to send the browser to."""

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthProfile:
        """Trade an authorization code for the caller's profile. Raises
        :class:`OAuthError` on failure — the router turns that into a problem
        response, never a 500."""
