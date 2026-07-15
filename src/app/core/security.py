"""Security primitives.

Part 3 (auth) adds JWT encode/decode, Argon2 hashing and refresh-token
rotation here. Until platform-staff accounts exist, platform routes are
guarded by a shared API key from settings — an explicit stopgap.
"""

import secrets
from typing import Annotated

from fastapi import Depends, Request

from app.core.config import Settings
from app.core.exceptions import UnauthorizedError

PLATFORM_KEY_HEADER = "x-platform-key"


def require_platform_key(request: Request) -> None:
    """Interim guard for `/api/v1/platform/*` until platform RBAC lands (Part 3)."""
    settings: Settings = request.app.state.settings
    provided = request.headers.get(PLATFORM_KEY_HEADER, "")
    if not secrets.compare_digest(provided, settings.platform_api_key):
        raise UnauthorizedError("A valid platform API key is required.")


PlatformKeyDep = Annotated[None, Depends(require_platform_key)]
