"""Google OAuth 2.0 adapter (§7.1).

Constructed **only** when both ``oauth_google_client_id`` and
``oauth_google_client_secret`` are configured — a half-configured client would
fail at the redirect, after the person has already left the site, which is a
much worse failure than the router saying "not configured" up front (same
fail-fast-at-construction rule as the storage credentials and the Anthropic
adapter).

The profile is read from Google's userinfo endpoint rather than by verifying
the ``id_token`` locally: the token comes back over a TLS connection to
Google's own token endpoint that we initiated, so a second signature check
buys nothing here and would pull in JWKS fetching, caching and rotation.
"""

from __future__ import annotations

from urllib.parse import urlencode

import httpx
import structlog
from authlib.integrations.httpx_client import AsyncOAuth2Client

from app.integrations.auth_oauth.base import OAuthError, OAuthProfile

logger = structlog.get_logger(__name__)

PROVIDER_KEY = "google"

_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
_SCOPE = "openid email profile"


class GoogleOAuthProvider:
    """An :class:`~app.integrations.auth_oauth.base.OAuthProvider` for Google."""

    def __init__(
        self, *, client_id: str, client_secret: str, timeout_seconds: float = 10.0
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("Google OAuth requires both a client id and a client secret.")
        self._client_id = client_id
        self._client_secret = client_secret
        self._timeout = timeout_seconds

    @property
    def key(self) -> str:
        return PROVIDER_KEY

    def authorization_url(self, *, redirect_uri: str, state: str) -> str:
        # Built directly rather than via an OAuth client: this is pure string
        # assembly (no network call), and instantiating AsyncOAuth2Client here
        # would leak an unclosed httpx client / connection pool per call.
        params = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": _SCOPE,
                "state": state,
            }
        )
        return f"{_AUTHORIZE_URL}?{params}"

    async def exchange_code(self, *, code: str, redirect_uri: str) -> OAuthProfile:
        try:
            async with AsyncOAuth2Client(
                client_id=self._client_id,
                client_secret=self._client_secret,
                redirect_uri=redirect_uri,
                timeout=self._timeout,
            ) as client:
                token = await client.fetch_token(
                    _TOKEN_URL, code=code, grant_type="authorization_code"
                )
                access_token = token.get("access_token")
                if not access_token:
                    raise OAuthError("Google returned no access token.", permanent=True)
                response = await client.get(_USERINFO_URL)
        except OAuthError:
            raise
        except httpx.HTTPError as exc:
            # Transport-level: worth a retry, unlike a rejected code.
            raise OAuthError(f"Google OAuth transport failure: {exc}") from exc
        except Exception as exc:
            raise OAuthError(f"Google OAuth exchange failed: {exc}", permanent=True) from exc

        if response.status_code != httpx.codes.OK:
            raise OAuthError(
                f"Google userinfo returned {response.status_code}.",
                permanent=response.status_code < 500,
            )
        data = response.json()
        subject = data.get("sub")
        if not subject:
            raise OAuthError("Google userinfo carried no subject.", permanent=True)
        return OAuthProfile(
            subject=str(subject),
            email=data.get("email"),
            email_verified=bool(data.get("email_verified")),
            first_name=data.get("given_name"),
            last_name=data.get("family_name"),
        )
