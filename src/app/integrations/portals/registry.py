"""Resolve a tenant's enabled portal adapters from its settings (§8.14).

Syndication is configured entirely through ``settings.syndication`` on the
tenant (same defensive-JSONB-settings pattern used for appointments/mortgage
defaults elsewhere) — no per-tenant secrets table this part. Shape::

    settings.syndication = {
        "mock": {
            "enabled": true,
            "base_url": "https://portal.example/api",
            "api_key": "..."          # optional
        }
    }

``KNOWN_PORTALS`` is the code-owned allowlist of portal keys we ship an adapter
for. A tenant can only enable a key that exists here; an unknown key in settings
is ignored (never trusted to name an adapter). This keeps the set of portals a
git-auditable constant, like the RBAC matrix.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import structlog

from app.integrations.portals.base import PortalAdapter
from app.integrations.portals.mock import MOCK_PORTAL_KEY, MockPortalAdapter

logger = structlog.get_logger(__name__)

# The code-owned set of portal keys we ship an adapter for. Adding a portal =
# adding an entry here + its adapter; a tenant cannot invent a key.
KNOWN_PORTALS: frozenset[str] = frozenset({MOCK_PORTAL_KEY})


def _syndication_settings(tenant_settings: Mapping[str, Any]) -> dict[str, Any]:
    raw = tenant_settings.get("syndication")
    return raw if isinstance(raw, dict) else {}


def _portal_config(tenant_settings: Mapping[str, Any], portal_key: str) -> dict[str, Any]:
    config = _syndication_settings(tenant_settings).get(portal_key)
    return config if isinstance(config, dict) else {}


def is_portal_enabled(tenant_settings: Mapping[str, Any], portal_key: str) -> bool:
    """Is ``portal_key`` a known portal switched on for this tenant?"""
    if portal_key not in KNOWN_PORTALS:
        return False
    return bool(_portal_config(tenant_settings, portal_key).get("enabled"))


def enabled_portal_keys(tenant_settings: Mapping[str, Any]) -> list[str]:
    """Every known portal the tenant has enabled — the fan-out target set."""
    return [key for key in sorted(KNOWN_PORTALS) if is_portal_enabled(tenant_settings, key)]


def build_adapter(tenant_settings: Mapping[str, Any], portal_key: str) -> PortalAdapter | None:
    """Construct the adapter for ``portal_key`` from the tenant's settings, or
    ``None`` if the portal is unknown, disabled, or misconfigured (e.g. missing
    ``base_url``) — the caller treats ``None`` as "nothing to sync"."""
    if not is_portal_enabled(tenant_settings, portal_key):
        return None
    config = _portal_config(tenant_settings, portal_key)

    if portal_key == MOCK_PORTAL_KEY:
        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not base_url:
            logger.warning("portal_misconfigured", portal_key=portal_key, reason="no base_url")
            return None
        api_key = config.get("api_key")
        return MockPortalAdapter(
            base_url, api_key=api_key if isinstance(api_key, str) and api_key else None
        )

    return None  # pragma: no cover — guarded by is_portal_enabled + KNOWN_PORTALS
