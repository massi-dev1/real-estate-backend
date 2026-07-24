"""OAuth provider resolution (§7.1).

Returns ``None`` when the named provider has no credentials configured — the
offline-safe default for this deployment. The auth router turns that ``None``
into a clear "not configured" problem response, so the feature is visibly off
rather than silently broken. Adding credentials flips it on with no code change.
"""

from app.core.config import Settings
from app.integrations.auth_oauth.base import OAuthProvider
from app.integrations.auth_oauth.google import PROVIDER_KEY as GOOGLE_KEY
from app.integrations.auth_oauth.google import GoogleOAuthProvider

# Providers this build knows how to construct. A provider key outside this set
# is unknown regardless of configuration (the same code-owned-allowlist stance
# as `KNOWN_PORTALS` in syndication).
KNOWN_OAUTH_PROVIDERS: frozenset[str] = frozenset({GOOGLE_KEY})


def build_oauth_provider(settings: Settings, key: str) -> OAuthProvider | None:
    """The configured provider for ``key``, or ``None`` if it is unknown or
    has no credentials."""
    if (
        key == GOOGLE_KEY
        and settings.oauth_google_client_id
        and settings.oauth_google_client_secret
    ):
        return GoogleOAuthProvider(
            client_id=settings.oauth_google_client_id,
            client_secret=settings.oauth_google_client_secret,
        )
    return None


def configured_oauth_providers(settings: Settings) -> list[str]:
    """Provider keys that are actually usable right now — what a frontend needs
    to decide which social buttons to render."""
    return [key for key in sorted(KNOWN_OAUTH_PROVIDERS) if build_oauth_provider(settings, key)]
