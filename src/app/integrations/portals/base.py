"""Portal syndication adapters — the integration seam (§8.14, §5 ``integrations/``).

This layer is **infrastructure**, not a tenant-facing feature module: it holds no
DB access, no RBAC, no router. It defines the common contract every target
portal (Ouedkniss, local MLS, an aggregator, …) is driven through, plus a
neutral :class:`PortalListing` payload the ``modules/syndication`` service maps a
``Listing`` ORM object into (adapters never import listings' models — the DTO is
the boundary).

Each adapter implements three verbs — ``push`` / ``update`` / ``remove`` —
returning a :class:`PortalResult`. Adapters are pure I/O against a remote portal;
retry/backoff, circuit-breaking and sync-state persistence live in the Celery
task and the syndication service that call them, so an adapter stays a thin,
testable translation of our payload into the portal's own API.

No real Algerian portal exposes a public partner API in this environment, so the
one adapter shipped here (:class:`MockPortalAdapter`) runs against a **documented
mock contract** (an HTTP endpoint accepting our JSON, echoing a ``remote_id``) —
clearly a stand-in, not a fabricated live integration. A real adapter slots in
behind the same protocol later.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable


class PortalError(Exception):
    """An adapter failed to reach or was rejected by the remote portal.

    ``permanent`` distinguishes a bad-payload rejection (the portal will never
    accept this listing as-is — do not retry, surface it) from a transient
    transport/5xx failure (retry with backoff). Mirrors the media pipeline's
    ``MediaValidationError`` vs. infrastructure-error split.
    """

    def __init__(self, message: str, *, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass(frozen=True, slots=True)
class PortalResult:
    """Outcome of one adapter call. ``remote_id`` is the portal's own id for the
    listing (persisted so later ``update``/``remove`` calls can reference it)."""

    remote_id: str | None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PortalListing:
    """Portal-neutral listing payload — the boundary DTO adapters translate from.

    The syndication service builds this from a ``Listing`` (via the listings
    boundary), so no adapter ever imports listings' models/repository. Only
    already-negotiated, primitive data crosses here; i18n text is pre-picked to
    a single locale by the caller.
    """

    listing_id: uuid.UUID
    reference_code: str
    title: str
    description: str
    purpose: str
    property_type: str
    price: Decimal
    currency: str
    beds: int | None
    baths: int | None
    area_built: Decimal | None
    address: dict[str, str]
    lat: float | None
    lng: float | None
    features: list[str] = field(default_factory=list)
    photo_urls: list[str] = field(default_factory=list)
    detail_url: str | None = None


@runtime_checkable
class PortalAdapter(Protocol):
    """The contract every portal adapter satisfies (§8.14).

    ``key`` is the stable identifier persisted in ``portal_sync_state.portal_key``
    and used as the tenant ``settings.syndication`` toggle key.
    """

    @property
    def key(self) -> str: ...

    async def push(self, listing: PortalListing) -> PortalResult:
        """Create the listing on the portal. Returns its remote id."""

    async def update(self, listing: PortalListing, *, remote_id: str) -> PortalResult:
        """Update an already-pushed listing referenced by ``remote_id``."""

    async def remove(self, *, remote_id: str) -> PortalResult:
        """Withdraw the listing from the portal."""


class PortalAction(enum.StrEnum):
    """Which adapter verb a sync task should invoke for a listing event."""

    PUSH = "push"
    UPDATE = "update"
    REMOVE = "remove"
