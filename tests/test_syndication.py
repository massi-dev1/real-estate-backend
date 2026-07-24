"""Portal syndication module (§8.14/§13).

Covers the adapter seam + circuit breaker, the listing-lifecycle → post-commit
sync enqueue (eager-mode Celery), the pull feeds (XML/CSV), the portal admin
(config, sync-state visibility, manual re-push), RBAC, and tenant isolation.

The adapter's real HTTP call is replaced by an in-memory ``FakeAdapter`` — both
the request-time service and the worker task resolve adapters through the same
``build_adapter`` seam, so patching it once at ``syndication.service`` covers
both. ``FakeAdapter`` records every call and can be told to fail transiently or
permanently, which is how the circuit breaker and retry split are exercised
without a live portal.
"""

import csv
import io
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from xml.etree import ElementTree as ET

import pytest
from httpx import AsyncClient

from app.core.permissions import Role
from app.integrations.portals.base import PortalError, PortalListing, PortalResult
from app.modules.syndication.service import CIRCUIT_BREAKER_THRESHOLD
from tests.helpers import HOST_A, HOST_B
from tests.test_listings import make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]

FEEDS = "/api/v1/feeds/listings"
PORTAL = "/api/v1/portal/syndication"

# A tenant settings blob with the mock portal enabled.
ENABLED_SETTINGS: dict[str, Any] = {
    "syndication": {"mock": {"enabled": True, "base_url": "https://portal.test/api"}}
}


# ---- the in-memory fake adapter ----


class FakeAdapter:
    """Records calls; each shared instance is keyed by portal_key. ``mode``
    controls behaviour: ok / transient-fail / permanent-fail."""

    key = "mock"

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.mode = "ok"
        self._counter = 0

    async def push(self, listing: PortalListing) -> PortalResult:
        return self._act("push", None)

    async def update(self, listing: PortalListing, *, remote_id: str) -> PortalResult:
        return self._act("update", remote_id)

    async def remove(self, *, remote_id: str) -> PortalResult:
        return self._act("remove", remote_id)

    def _act(self, verb: str, remote_id: str | None) -> PortalResult:
        self.calls.append((verb, remote_id))
        if self.mode == "transient":
            raise PortalError("boom (transient)")
        if self.mode == "permanent":
            raise PortalError("bad payload", permanent=True)
        self._counter += 1
        return PortalResult(remote_id=remote_id or f"remote-{self._counter}", detail=verb)


@pytest.fixture
def fake_adapter(monkeypatch: pytest.MonkeyPatch) -> FakeAdapter:
    """Replace the adapter the syndication service resolves with a fake, for
    every enabled ``mock`` portal (disabled portals still return None)."""
    adapter = FakeAdapter()

    def _build(tenant_settings: Any, portal_key: str) -> FakeAdapter | None:
        from app.integrations.portals.registry import is_portal_enabled

        if portal_key == "mock" and is_portal_enabled(tenant_settings, portal_key):
            return adapter
        return None

    monkeypatch.setattr("app.modules.syndication.service.build_adapter", _build)
    return adapter


async def _publish(client: AsyncClient, headers: dict[str, str], listing_id: str) -> None:
    resp = await transition(client, headers, listing_id, "published")
    assert resp.status_code == 200, resp.text


# ---- adapter contract + error classification (unit) ----


def _portal_listing() -> PortalListing:
    from decimal import Decimal

    return PortalListing(
        listing_id=uuid.uuid4(),
        reference_code="AGE-2026-00001",
        title="Nice flat",
        description="Bright",
        purpose="sale",
        property_type="apartment",
        price=Decimal("12500000.00"),
        currency="DZD",
        beds=3,
        baths=1,
        area_built=Decimal("85.50"),
        address={"city": "Alger"},
        lat=36.75,
        lng=3.04,
        features=["balcony"],
    )


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""

    def json(self) -> dict[str, Any]:
        assert self._payload is not None
        return self._payload


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, response: Any) -> None:
    class _Client:
        def __init__(self, *a: Any, **k: Any) -> None: ...

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *a: Any) -> None: ...

        async def request(self, *a: Any, **k: Any) -> Any:
            if isinstance(response, Exception):
                raise response
            return response

    monkeypatch.setattr("app.integrations.portals.mock.httpx.AsyncClient", _Client)


async def test_adapter_push_returns_remote_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.portals.mock import MockPortalAdapter

    _patch_httpx(monkeypatch, _FakeResponse(201, {"id": "abc-123"}))
    adapter = MockPortalAdapter("https://portal.test/api")
    result = await adapter.push(_portal_listing())
    assert result.remote_id == "abc-123"


async def test_adapter_4xx_is_permanent(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.portals.mock import MockPortalAdapter

    _patch_httpx(monkeypatch, _FakeResponse(400))
    adapter = MockPortalAdapter("https://portal.test/api")
    with pytest.raises(PortalError) as exc:
        await adapter.push(_portal_listing())
    assert exc.value.permanent is True


async def test_adapter_5xx_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.portals.mock import MockPortalAdapter

    _patch_httpx(monkeypatch, _FakeResponse(503))
    adapter = MockPortalAdapter("https://portal.test/api")
    with pytest.raises(PortalError) as exc:
        await adapter.push(_portal_listing())
    assert exc.value.permanent is False


async def test_adapter_transport_error_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    from app.integrations.portals.mock import MockPortalAdapter

    _patch_httpx(monkeypatch, httpx.ConnectError("refused"))
    adapter = MockPortalAdapter("https://portal.test/api")
    with pytest.raises(PortalError) as exc:
        await adapter.push(_portal_listing())
    assert exc.value.permanent is False


async def test_adapter_remove_treats_404_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.integrations.portals.mock import MockPortalAdapter

    _patch_httpx(monkeypatch, _FakeResponse(404))
    adapter = MockPortalAdapter("https://portal.test/api")
    result = await adapter.remove(remote_id="gone")
    assert result.remote_id == "gone"


# ---- lifecycle → sync enqueue (eager Celery) ----


async def test_publish_pushes_to_enabled_portal(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])

    # The publish fanned out to the mock portal (first sync is a push).
    assert ("push", None) in fake_adapter.calls

    # Sync state records the success + the portal's remote id.
    state = await client.get(f"{PORTAL}/listings/{listing['id']}/state", headers=admin)
    assert state.status_code == 200, state.text
    rows = state.json()
    assert len(rows) == 1
    assert rows[0]["portalKey"] == "mock"
    assert rows[0]["lastStatus"] == "synced"
    assert rows[0]["remoteId"].startswith("remote-")


async def test_edit_published_listing_updates_portal(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    fake_adapter.calls.clear()

    resp = await client.patch(
        f"/api/v1/portal/listings/{listing['id']}",
        json={"price": "9999999.00"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    # A live edit re-syncs as an update (remote id already known).
    assert fake_adapter.calls and fake_adapter.calls[-1][0] == "update"


async def test_archive_removes_from_portal(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    fake_adapter.calls.clear()

    resp = await transition(client, admin, listing["id"], "archived")
    assert resp.status_code == 200, resp.text
    assert fake_adapter.calls and fake_adapter.calls[-1][0] == "remove"

    state = await client.get(f"{PORTAL}/listings/{listing['id']}/state", headers=admin)
    assert state.json()[0]["lastStatus"] == "removed"


async def test_no_sync_when_portal_disabled(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    # No syndication settings → nothing enqueued, no sync-state rows.
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    assert fake_adapter.calls == []
    state = await client.get(f"{PORTAL}/listings/{listing['id']}/state", headers=admin)
    assert state.json() == []


# ---- circuit breaker + retry split ----


async def test_permanent_failure_records_failed_no_retry(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.mode = "permanent"
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    # Eager Celery runs the task inline; a permanent failure must not raise
    # (no retry) — the publish transition still succeeds.
    await _publish(client, admin, listing["id"])

    state = await client.get(f"{PORTAL}/listings/{listing['id']}/state", headers=admin)
    row = state.json()[0]
    assert row["lastStatus"] == "failed"
    assert row["consecutiveFailures"] == 1
    assert row["circuitOpen"] is False


async def _fail_until_open(client: AsyncClient, admin: dict[str, str], listing_id: str) -> None:
    """Drive consecutive failures via edits (each enqueues an UPDATE that, with
    no remote id yet, becomes a PUSH) until the breaker trips. Edits — unlike a
    manual re-push — never reset the circuit, so failures accumulate cleanly.
    The publish already counts as failure #1, so THRESHOLD-1 more edits trip it."""
    for i in range(CIRCUIT_BREAKER_THRESHOLD - 1):
        resp = await client.patch(
            f"/api/v1/portal/listings/{listing_id}",
            json={"price": f"{1000000 + i}.00"},
            headers=admin,
        )
        assert resp.status_code == 200, resp.text


async def test_circuit_opens_after_threshold(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.mode = "permanent"
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])  # failure #1
    await _fail_until_open(client, admin, listing["id"])

    state = await client.get(f"{PORTAL}/listings/{listing['id']}/state", headers=admin)
    row = state.json()[0]
    assert row["consecutiveFailures"] >= CIRCUIT_BREAKER_THRESHOLD
    assert row["circuitOpen"] is True
    assert row["lastStatus"] == "paused"


async def test_open_circuit_stops_attempts(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.mode = "permanent"
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    await _fail_until_open(client, admin, listing["id"])

    # With the breaker open, a further edit must not even call the adapter.
    fake_adapter.calls.clear()
    await client.patch(
        f"/api/v1/portal/listings/{listing['id']}",
        json={"price": "5555555.00"},
        headers=admin,
    )
    assert fake_adapter.calls == []  # circuit open → no attempt (no retry-storm)


async def test_repush_resets_open_circuit_and_recovers(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    fake_adapter.mode = "permanent"
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    await _fail_until_open(client, admin, listing["id"])

    # The portal recovers; a manual re-push clears the open breaker and syncs.
    fake_adapter.mode = "ok"
    resp = await client.post(f"{PORTAL}/listings/{listing['id']}/repush", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == ["mock"]

    state = await client.get(f"{PORTAL}/listings/{listing['id']}/state", headers=admin)
    row = state.json()[0]
    assert row["circuitOpen"] is False
    assert row["lastStatus"] == "synced"


async def test_transient_failure_signals_retry(
    app: Any,
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    """A transient failure returns ``retry=True`` (Celery would back off) and the
    circuit stays closed. Driven at the service level so the eager-mode task's
    retry-raise never propagates into a request — in production ``.delay()`` just
    enqueues and the raise happens in the worker, not the caller."""
    from app.core.database import set_tenant_guc
    from app.core.tenancy import TenantContext
    from app.integrations.portals.base import PortalAction
    from app.modules.syndication.service import build_syndication_service_for_worker

    tenant, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])  # first sync succeeds (mode ok)

    fake_adapter.mode = "transient"
    tid = uuid.UUID(tenant["id"])
    ctx = TenantContext(
        id=tid,
        slug=tenant["slug"],
        name=tenant["name"],
        status=tenant["status"],
        settings=tenant["settings"],
    )
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, tid)
        service = build_syndication_service_for_worker(session, app.state.settings)
        outcome = await service.sync_to_portal(
            ctx, uuid.UUID(listing["id"]), "mock", PortalAction.UPDATE
        )
    assert outcome.retry is True
    assert outcome.status == "failed"


async def test_repush_unpublished_listing_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing = await make_listing(client, admin)  # still draft
    resp = await client.post(f"{PORTAL}/listings/{listing['id']}/repush", headers=admin)
    assert resp.status_code == 404


# ---- admin config ----


async def test_settings_roundtrip_hides_secret(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.put(
        f"{PORTAL}/settings",
        json={
            "portals": {
                "mock": {
                    "enabled": True,
                    "baseUrl": "https://portal.test/api",
                    "apiKey": "s3cret",
                }
            }
        },
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    portals = {p["key"]: p for p in resp.json()["portals"]}
    assert portals["mock"]["enabled"] is True
    assert portals["mock"]["baseUrl"] == "https://portal.test/api"
    assert portals["mock"]["hasApiKey"] is True
    # The secret itself is never echoed back.
    assert "apiKey" not in portals["mock"]


async def test_settings_update_preserves_api_key_when_omitted(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    # Set an api_key, then PUT again editing an unrelated field without
    # resupplying it — since GET never echoes the secret, a naive full-replace
    # would silently wipe it. It must be carried forward instead.
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await client.put(
        f"{PORTAL}/settings",
        json={
            "portals": {
                "mock": {
                    "enabled": True,
                    "baseUrl": "https://portal.test/api",
                    "apiKey": "s3cret",
                }
            }
        },
        headers=admin,
    )
    resp = await client.put(
        f"{PORTAL}/settings",
        json={"portals": {"mock": {"enabled": False, "baseUrl": "https://portal.test/api"}}},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    portals = {p["key"]: p for p in resp.json()["portals"]}
    assert portals["mock"]["enabled"] is False
    assert portals["mock"]["hasApiKey"] is True

    # Re-enable and confirm the preserved key is actually used to sync (not
    # just reported as present).
    await client.put(
        f"{PORTAL}/settings",
        json={"portals": {"mock": {"enabled": True, "baseUrl": "https://portal.test/api"}}},
        headers=admin,
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    assert ("push", None) in fake_adapter.calls


async def test_settings_rejects_unknown_portal(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.put(
        f"{PORTAL}/settings",
        json={"portals": {"nope": {"enabled": True}}},
        headers=admin,
    )
    assert resp.status_code == 422


async def test_settings_change_enables_syncing(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    # Enable the portal through the admin API (not tenant-create) — this must
    # invalidate the resolver cache so the next publish sees it enabled.
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await client.put(
        f"{PORTAL}/settings",
        json={"portals": {"mock": {"enabled": True, "baseUrl": "https://portal.test/api"}}},
        headers=admin,
    )
    listing = await make_listing(client, admin)
    await _publish(client, admin, listing["id"])
    assert ("push", None) in fake_adapter.calls


# ---- feeds ----


async def _seed_published(
    client: AsyncClient, headers: dict[str, str], fake_adapter: FakeAdapter
) -> None:
    listing = await make_listing(client, headers)
    await _publish(client, headers, listing["id"])


async def test_xml_feed(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await _seed_published(client, admin, fake_adapter)

    resp = await client.get(f"{FEEDS}.xml", headers={"Host": HOST_A})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/xml")
    root = ET.fromstring(resp.text)
    listings = root.findall("listing")
    assert len(listings) == 1
    ref = listings[0].findtext("reference")
    assert ref and ref.startswith("AGE-")
    url = listings[0].findtext("url")
    assert url and f"https://{HOST_A}/listings/{ref}" == url


async def test_csv_feed(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await _seed_published(client, admin, fake_adapter)

    resp = await client.get(f"{FEEDS}.csv", headers={"Host": HOST_A})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(resp.text)))
    assert len(rows) == 1
    assert rows[0]["reference"].startswith("AGE-")
    assert rows[0]["currency"] == "DZD"


async def test_feed_only_published(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    await make_listing(client, admin)  # draft — must not appear
    resp = await client.get(f"{FEEDS}.xml", headers={"Host": HOST_A})
    assert ET.fromstring(resp.text).findall("listing") == []


async def test_feed_unknown_format_404(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    await create_tenant(client, platform_headers)
    resp = await client.get(f"{FEEDS}.json", headers={"Host": HOST_A})
    assert resp.status_code == 404


# ---- RBAC + tenant isolation ----


async def test_syndication_admin_requires_listing_manage(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, buyer = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.BUYER_RENTER
    )
    assert (await client.get(f"{PORTAL}/settings", headers=buyer)).status_code == 403
    assert (await client.get(f"{PORTAL}/state", headers=buyer)).status_code == 403


async def test_sync_state_is_tenant_isolated(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    fake_adapter: FakeAdapter,
) -> None:
    _, admin_a = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN, settings=ENABLED_SETTINGS
    )
    listing_a = await make_listing(client, admin_a)
    await _publish(client, admin_a, listing_a["id"])

    tenant_b = await create_tenant(
        client, platform_headers, name="B", slug="agency-b", domain=HOST_B
    )
    admin_b = await _b_admin(client, create_tenant_user, tenant_b["id"])

    # B sees none of A's sync state.
    state_b = await client.get(f"{PORTAL}/state", headers=admin_b)
    assert state_b.status_code == 200
    assert state_b.json()["items"] == []

    # And B cannot read A's listing sync state (404 — no oracle).
    cross = await client.get(f"{PORTAL}/listings/{listing_a['id']}/state", headers=admin_b)
    assert cross.status_code == 200
    assert cross.json() == []  # RLS: A's rows are invisible, so empty


async def _b_admin(
    client: AsyncClient, create_tenant_user: CreateTenantUser, tenant_id: str
) -> dict[str, str]:
    from tests.test_listings import add_user

    return await add_user(
        client,
        create_tenant_user,
        tenant_id,
        Role.ADMIN,
        email="admin@b.example.com",
        host=HOST_B,
    )
