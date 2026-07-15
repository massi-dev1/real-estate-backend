"""Cursor (keyset) pagination helpers — the default for every list endpoint (§9).

A cursor is an opaque urlsafe-base64 JSON object holding the keyset values of
the last item returned (e.g. ``{"published_at": ..., "id": ...}``). Repositories
build the actual keyset WHERE clause; these helpers only encode/decode and
define the response envelope.
"""

import base64
import binascii
import json
from typing import Any

from fastapi import status

from app.core.exceptions import AppError
from app.core.schema import OutSchema

DEFAULT_PAGE_SIZE = 24
MAX_PAGE_SIZE = 100


class InvalidCursorError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    slug = "invalid-cursor"
    title = "Invalid Pagination Cursor"


def encode_cursor(values: dict[str, Any]) -> str:
    raw = json.dumps(values, separators=(",", ":"), default=str).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()))
    except (binascii.Error, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidCursorError("The provided cursor is malformed.") from exc
    if not isinstance(decoded, dict):
        raise InvalidCursorError("The provided cursor is malformed.")
    return decoded


def clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_PAGE_SIZE
    return max(1, min(limit, MAX_PAGE_SIZE))


class Page[T](OutSchema):
    """Standard list-endpoint envelope: ``{items, nextCursor, totalEstimate}``."""

    items: list[T]
    next_cursor: str | None = None
    total_estimate: int | None = None
