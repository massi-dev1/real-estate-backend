"""Caching & performance (§11, Part 32).

Two layers:

* **Redis cache-aside** (``core/cache.py``) — versioned-key invalidation, a
  hit/miss counter, and degrade-open behaviour. Exercised as a unit *and*
  end-to-end through ``GET /site/config`` and the public content page.
* **HTTP validator caching** (``core/http_cache.py``) — ``ETag`` +
  ``Cache-Control`` on public GETs and a ``304`` on a matching
  ``If-None-Match``, verified on the listing detail and content page.
"""

from typing import Any

from httpx import AsyncClient

from app.core.cache import bump_version, cache_aside, value_key
from app.core.metrics import CACHE_LOOKUPS
from app.core.permissions import Role
from tests.test_content import PAGE_BODY, make_page
from tests.test_listings import make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

HOST_A = "agency-a.test"


def cache_count(entity: str, result: str) -> float:
    return float(CACHE_LOOKUPS.labels(entity=entity, result=result)._value.get())


# ---------------------------------------------------------------------------
# core/cache.py — unit
# ---------------------------------------------------------------------------


async def test_cache_aside_hit_skips_loader(app: Any) -> None:
    redis = app.state.redis
    calls = {"n": 0}

    async def loader() -> dict[str, int]:
        calls["n"] += 1
        return {"v": calls["n"]}

    kwargs: dict[str, Any] = {
        "tenant_id": "t1",
        "entity": "unit_hit",
        "ident": "_",
        "ttl_seconds": 60,
        "loader": loader,
    }
    first = await cache_aside(redis, **kwargs)
    second = await cache_aside(redis, **kwargs)

    assert first == {"v": 1}
    assert second == {"v": 1}  # served from Redis — loader ran once
    assert calls["n"] == 1


async def test_cache_aside_version_bump_invalidates(app: Any) -> None:
    redis = app.state.redis
    calls = {"n": 0}

    async def loader() -> int:
        calls["n"] += 1
        return calls["n"]

    kwargs: dict[str, Any] = {
        "tenant_id": "t2",
        "entity": "unit_bump",
        "ident": "_",
        "ttl_seconds": 60,
        "loader": loader,
    }
    assert await cache_aside(redis, **kwargs) == 1
    assert await cache_aside(redis, **kwargs) == 1  # cached

    await bump_version(redis, "t2", "unit_bump")
    assert await cache_aside(redis, **kwargs) == 2  # new version → miss → reload
    assert calls["n"] == 2


async def test_cache_aside_records_hit_and_miss_metrics(app: Any) -> None:
    redis = app.state.redis
    before_hit = cache_count("unit_metric", "hit")
    before_miss = cache_count("unit_metric", "miss")

    async def loader() -> str:
        return "x"

    kwargs: dict[str, Any] = {
        "tenant_id": "t3",
        "entity": "unit_metric",
        "ident": "_",
        "ttl_seconds": 60,
        "loader": loader,
    }
    await cache_aside(redis, **kwargs)  # miss
    await cache_aside(redis, **kwargs)  # hit

    assert cache_count("unit_metric", "miss") == before_miss + 1
    assert cache_count("unit_metric", "hit") == before_hit + 1


async def test_cache_aside_degrades_open_without_redis() -> None:
    calls = {"n": 0}

    async def loader() -> str:
        calls["n"] += 1
        return "y"

    # redis=None → the loader runs uncached, every time.
    args: dict[str, Any] = {
        "tenant_id": "t",
        "entity": "e",
        "ident": "_",
        "ttl_seconds": 60,
        "loader": loader,
    }
    assert await cache_aside(None, **args) == "y"
    assert await cache_aside(None, **args) == "y"
    assert calls["n"] == 2


async def test_cache_aside_degrades_open_on_redis_error() -> None:
    class BrokenRedis:
        async def get(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("down")

        async def set(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("down")

    calls = {"n": 0}

    async def loader() -> str:
        calls["n"] += 1
        return "z"

    got = await cache_aside(
        BrokenRedis(), tenant_id="t", entity="e", ident="_", ttl_seconds=60, loader=loader
    )
    assert got == "z"
    assert calls["n"] == 1  # the loader still produced the value


async def test_cache_aside_corrupt_blob_is_a_miss(app: Any) -> None:
    redis = app.state.redis

    async def loader() -> dict[str, int]:
        return {"ok": 1}

    kwargs: dict[str, Any] = {
        "tenant_id": "t4",
        "entity": "unit_corrupt",
        "ident": "_",
        "ttl_seconds": 60,
        "loader": loader,
    }
    await cache_aside(redis, **kwargs)  # populate v0
    # Overwrite the stored value with non-JSON garbage.
    await redis.set(value_key("t4", "unit_corrupt", "_", 0), b"not json{")
    assert await cache_aside(redis, **kwargs) == {"ok": 1}  # recomputed, not a 500


# ---------------------------------------------------------------------------
# /site/config — end-to-end cache_aside + write invalidation
# ---------------------------------------------------------------------------


async def test_site_config_cached_and_invalidated_on_settings_write(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    tenant = await create_tenant(client, platform_headers, settings={"brand": "blue"})
    before_hit = cache_count("site_config", "hit")

    first = await client.get("/api/v1/site/config", headers={"Host": HOST_A})
    assert first.status_code == 200
    assert first.json()["settings"] == {"brand": "blue"}

    second = await client.get("/api/v1/site/config", headers={"Host": HOST_A})
    assert second.json() == first.json()
    assert cache_count("site_config", "hit") == before_hit + 1  # second read hit Redis

    # A platform settings PATCH bumps the site_config version → next read misses
    # and reflects the change.
    patched = await client.patch(
        f"/api/v1/platform/tenants/{tenant['id']}",
        json={"settings": {"brand": "green"}},
        headers=platform_headers,
    )
    assert patched.status_code == 200
    after = await client.get("/api/v1/site/config", headers={"Host": HOST_A})
    assert after.json()["settings"] == {"brand": "green"}


# ---------------------------------------------------------------------------
# public content page — cache_aside + publish invalidation + ETag/304
# ---------------------------------------------------------------------------


async def _publish_page(
    client: AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    page = await make_page(client, headers, **overrides)
    resp = await client.post(f"/api/v1/portal/content/pages/{page['id']}/publish", headers=headers)
    assert resp.status_code == 200, resp.text
    return page


async def test_public_page_cached_and_invalidated_on_republish(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: Any,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    page = await _publish_page(client, admin)
    url = f"/api/v1/pages/{page['slug']}"

    before_hit = cache_count("content_page", "hit")
    first = await client.get(url, headers={"Host": HOST_A})
    assert first.status_code == 200
    # Public view negotiates one locale per i18n field (default fr).
    assert first.json()["title"] == PAGE_BODY["title"]["fr"]

    second = await client.get(url, headers={"Host": HOST_A})
    assert second.json() == first.json()
    assert cache_count("content_page", "hit") == before_hit + 1

    # Edit the page → the content_page version bumps → next public read reflects it.
    edited = await client.patch(
        f"/api/v1/portal/content/pages/{page['id']}",
        json={"title": {"fr": "Nouveau titre", "en": "New title"}},
        headers=admin,
    )
    assert edited.status_code == 200
    after = await client.get(url, headers={"Host": HOST_A})
    assert after.json()["title"] == "Nouveau titre"


async def test_public_page_carries_etag_and_returns_304(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: Any,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    page = await _publish_page(client, admin)
    url = f"/api/v1/pages/{page['slug']}"

    resp = await client.get(url, headers={"Host": HOST_A})
    assert resp.status_code == 200
    etag = resp.headers["etag"]
    assert etag
    assert resp.headers["cache-control"] == "public, s-maxage=60"
    assert "Accept-Language" in resp.headers["vary"]

    # Conditional GET with the matching ETag → 304, no body.
    conditional = await client.get(url, headers={"Host": HOST_A, "If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.content == b""
    assert conditional.headers["etag"] == etag

    # A stale ETag → full 200 again.
    stale = await client.get(url, headers={"Host": HOST_A, "If-None-Match": '"deadbeef"'})
    assert stale.status_code == 200


# ---------------------------------------------------------------------------
# public listing detail — ETag + Last-Modified + Cache-Control + 304
# ---------------------------------------------------------------------------


async def test_public_listing_detail_http_cache(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: Any,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    url = f"/api/v1/listings/{listing['referenceCode']}"
    resp = await client.get(url, headers={"Host": HOST_A})
    assert resp.status_code == 200
    assert resp.json()["referenceCode"] == listing["referenceCode"]
    etag = resp.headers["etag"]
    assert etag
    assert resp.headers["cache-control"] == "public, s-maxage=60"
    assert "last-modified" in resp.headers

    conditional = await client.get(url, headers={"Host": HOST_A, "If-None-Match": etag})
    assert conditional.status_code == 304
    assert conditional.content == b""

    # If-Modified-Since with a future date → 304 as well.
    since = await client.get(
        url,
        headers={"Host": HOST_A, "If-Modified-Since": "Sat, 01 Jan 2050 00:00:00 GMT"},
    )
    assert since.status_code == 304


# ---------------------------------------------------------------------------
# map clusters — cache_aside (TTL-only)
# ---------------------------------------------------------------------------


async def test_map_clusters_cached(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: Any,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    before_hit = cache_count("listing_map", "hit")
    params = {"inBbox": "2.0,36.0,4.0,37.5"}
    first = await client.get("/api/v1/listings/map", params=params, headers={"Host": HOST_A})
    assert first.status_code == 200

    second = await client.get("/api/v1/listings/map", params=params, headers={"Host": HOST_A})
    assert second.json() == first.json()
    assert cache_count("listing_map", "hit") == before_hit + 1


# ---------------------------------------------------------------------------
# cache disabled → no caching, loader every time
# ---------------------------------------------------------------------------


async def test_cache_disabled_falls_through(app: Any) -> None:
    redis = app.state.redis
    calls = {"n": 0}

    async def loader() -> int:
        calls["n"] += 1
        return calls["n"]

    kwargs: dict[str, Any] = {
        "tenant_id": "t5",
        "entity": "unit_disabled",
        "ident": "_",
        "ttl_seconds": 60,
        "loader": loader,
    }
    assert await cache_aside(redis, enabled=False, **kwargs) == 1
    assert await cache_aside(redis, enabled=False, **kwargs) == 2
    assert calls["n"] == 2
