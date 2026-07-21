"""A single portal adapter running against a **documented mock contract** (§8.14).

No real Algerian portal exposes a partner API in this environment, so rather than
fabricate a fake integration this adapter targets a small, explicit HTTP contract
that a mock server (or a real portal that later adopts the same shape) can honour:

    POST   {base_url}/listings              body: PortalListing JSON  → 201 {"id": "<remote_id>"}
    PUT    {base_url}/listings/{remote_id}  body: PortalListing JSON  → 200 {"id": "<remote_id>"}
    DELETE {base_url}/listings/{remote_id}                            → 204

Rules the adapter enforces, mirroring the codebase's error stances:

- 4xx (except 429) → :class:`PortalError` with ``permanent=True`` — the portal
  rejected the payload; retrying is pointless (same split as the media pipeline's
  validation-vs-infrastructure errors). 408/425/429/5xx and transport errors →
  transient ``PortalError`` (retry with backoff).
- An ``api_key`` (from tenant settings) is sent as a bearer header when present.

A real adapter for a specific portal replaces this file's translation of
:class:`PortalListing` into that portal's own field names; the interface it
satisfies does not change.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import structlog

from app.integrations.portals.base import PortalError, PortalListing, PortalResult

logger = structlog.get_logger(__name__)

# Portal calls are I/O against a third party; keep them well under the >200ms
# request budget's cousin — but this runs in a Celery task, not a request, so the
# bound is about not hanging a worker, not user latency.
_TIMEOUT = httpx.Timeout(15.0)

# Status codes that mean "try again later" rather than "this will never work".
_TRANSIENT_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})

MOCK_PORTAL_KEY = "mock"


def _payload(listing: PortalListing) -> dict[str, Any]:
    """PortalListing → the mock contract's JSON body. A real adapter maps these
    keys onto the target portal's own schema instead."""

    def _num(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None

    return {
        "reference": listing.reference_code,
        "title": listing.title,
        "description": listing.description,
        "purpose": listing.purpose,
        "propertyType": listing.property_type,
        "price": str(listing.price),
        "currency": listing.currency,
        "beds": listing.beds,
        "baths": listing.baths,
        "areaBuilt": _num(listing.area_built),
        "address": listing.address,
        "lat": listing.lat,
        "lng": listing.lng,
        "features": listing.features,
        "photos": listing.photo_urls,
        "url": listing.detail_url,
    }


class MockPortalAdapter:
    """Drives the documented mock-portal HTTP contract above."""

    key = MOCK_PORTAL_KEY

    def __init__(self, base_url: str, *, api_key: str | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def push(self, listing: PortalListing) -> PortalResult:
        data = await self._request("POST", "/listings", json=_payload(listing), expect=(200, 201))
        remote_id = str(data.get("id")) if data.get("id") is not None else None
        if remote_id is None:
            raise PortalError("portal did not return a remote id", permanent=True)
        return PortalResult(remote_id=remote_id, detail="pushed")

    async def update(self, listing: PortalListing, *, remote_id: str) -> PortalResult:
        await self._request(
            "PUT", f"/listings/{remote_id}", json=_payload(listing), expect=(200, 204)
        )
        return PortalResult(remote_id=remote_id, detail="updated")

    async def remove(self, *, remote_id: str) -> PortalResult:
        await self._request("DELETE", f"/listings/{remote_id}", expect=(200, 204, 404))
        # A 404 on remove is success: the listing is already gone from the portal.
        return PortalResult(remote_id=remote_id, detail="removed")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expect: tuple[int, ...],
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.request(method, url, json=json, headers=self._headers)
        except httpx.HTTPError as exc:
            # Transport-level failure — transient, retry with backoff.
            raise PortalError(f"portal unreachable: {exc}") from exc

        if resp.status_code in expect:
            if not resp.content:
                return {}
            try:
                body = resp.json()
            except ValueError:
                return {}
            return body if isinstance(body, dict) else {}

        transient = resp.status_code in _TRANSIENT_STATUSES
        raise PortalError(
            f"portal returned {resp.status_code}",
            permanent=not transient,
        )
