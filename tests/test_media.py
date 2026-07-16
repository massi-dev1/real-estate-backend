"""Media pipeline (§8.2/§13): presigned upload flow, Celery processing
(magic bytes, variants, blurhash), embeds, cover rules, private documents,
public output, quotas and scoping. Runs against real MinIO + eager Celery."""

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pyvips
from httpx import AsyncClient

from app.core.permissions import Role
from tests.test_listings import add_user, make_listing, tenant_and_login, transition
from tests.test_tenants_platform_api import create_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]


def jpeg_bytes(width: int = 640, height: int = 480) -> bytes:
    # pyvips overloads `+` as per-band add — RUF005's unpacking "fix" would break it.
    image = pyvips.Image.black(width, height) + [180, 90, 30]  # noqa: RUF005
    return bytes(image.write_to_buffer(".jpg", Q=90))


def png_bytes() -> bytes:
    image = pyvips.Image.black(64, 64) + [10, 200, 120]  # noqa: RUF005
    return bytes(image.write_to_buffer(".png"))


PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF\n"


async def request_upload(
    client: AsyncClient,
    headers: dict[str, str],
    listing_id: str,
    *,
    kind: str = "photo",
    content_type: str = "image/jpeg",
    size_bytes: int = 1024,
) -> httpx.Response:
    return await client.post(
        f"/api/v1/portal/listings/{listing_id}/media/uploads",
        json={"kind": kind, "contentType": content_type, "sizeBytes": size_bytes},
        headers=headers,
    )


async def upload_and_confirm(
    client: AsyncClient,
    headers: dict[str, str],
    listing_id: str,
    data: bytes,
    *,
    kind: str = "photo",
    content_type: str = "image/jpeg",
) -> dict[str, Any]:
    """Full §8.2 flow: presign → PUT straight to storage → confirm.

    Celery eager mode processes the upload inline during confirm's
    post-commit hook; the returned dict is the row's *final* state.
    """
    resp = await request_upload(
        client, headers, listing_id, kind=kind, content_type=content_type, size_bytes=len(data)
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    media_id = body["media"]["id"]
    assert body["media"]["status"] == "pending"

    async with httpx.AsyncClient() as direct:
        put = await direct.put(body["uploadUrl"], content=data, headers=body["uploadHeaders"])
        assert put.status_code == 200, put.text

    confirm = await client.post(f"/api/v1/portal/media/{media_id}/confirm", headers=headers)
    assert confirm.status_code == 202, confirm.text
    assert confirm.json()["status"] == "processing"

    listed = await client.get(f"/api/v1/portal/listings/{listing_id}/media", headers=headers)
    assert listed.status_code == 200, listed.text
    row = next(m for m in listed.json() if m["id"] == media_id)
    return dict(row)


# ---- upload + processing ----


async def test_photo_upload_processes_variants_and_blurhash(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    media = await upload_and_confirm(client, admin, listing["id"], jpeg_bytes(800, 600))

    assert media["status"] == "ready"
    assert media["blurhash"]
    assert media["error"] is None
    # 4 widths x (webp + jpeg); a 1920w original is not required — small
    # sources are never upscaled, but every variant name still exists.
    assert set(media["variants"]) == {
        f"{name}_{fmt}"
        for name in ("thumb", "card", "gallery", "full")
        for fmt in ("webp", "jpeg")
    }
    thumb = media["variants"]["thumb_webp"]
    assert thumb["width"] == 320
    assert thumb["url"].startswith("http://localhost:9000/media-test/")

    # The public variant is anonymously fetchable (CDN stand-in) and carries
    # no metadata block (EXIF GPS stripped, §8.2).
    async with httpx.AsyncClient() as direct:
        got = await direct.get(thumb["url"])
        assert got.status_code == 200
        assert got.headers["content-type"] == "image/webp"
        assert b"Exif" not in got.content


async def test_magic_byte_mismatch_marks_failed(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    # Declared JPEG, actually PNG — extension/headers are claims, not proof.
    media = await upload_and_confirm(client, admin, listing["id"], png_bytes())

    assert media["status"] == "failed"
    assert "declared content type" in media["error"]
    assert media["variants"] == {}


async def test_confirm_without_upload_marks_failed(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    resp = await request_upload(client, admin, listing["id"])
    media_id = resp.json()["media"]["id"]
    confirm = await client.post(f"/api/v1/portal/media/{media_id}/confirm", headers=admin)
    assert confirm.status_code == 202

    listed = await client.get(f"/api/v1/portal/listings/{listing['id']}/media", headers=admin)
    row = next(m for m in listed.json() if m["id"] == media_id)
    assert row["status"] == "failed"
    assert "no uploaded file" in row["error"]

    # A processed (or failed) upload cannot be re-confirmed.
    again = await client.post(f"/api/v1/portal/media/{media_id}/confirm", headers=admin)
    assert again.status_code == 409


async def test_photo_quota_and_size_cap(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(
        client,
        platform_headers,
        create_tenant_user,
        Role.ADMIN,
        settings={"media": {"max_photos_per_listing": 2}},
    )
    listing = await make_listing(client, admin)

    for _ in range(2):
        resp = await request_upload(client, admin, listing["id"])
        assert resp.status_code == 201
    # Pending slots reserve quota — a third presign is refused outright.
    third = await request_upload(client, admin, listing["id"])
    assert third.status_code == 403
    assert "quota" in third.json()["detail"]

    over = await request_upload(
        client, admin, listing["id"], size_bytes=26 * 1024 * 1024
    )
    assert over.status_code == 403


# ---- embeds ----


async def test_embed_flow_and_validation(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    embeds_url = f"/api/v1/portal/listings/{listing['id']}/media/embeds"

    ok = await client.post(
        embeds_url,
        json={"kind": "video", "url": "https://www.youtube.com/watch?v=abc123"},
        headers=admin,
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["status"] == "ready"  # nothing to process
    assert ok.json()["embedUrl"] == "https://www.youtube.com/watch?v=abc123"

    for bad in (
        {"kind": "video", "url": "http://www.youtube.com/watch?v=abc"},  # not https
        {"kind": "video", "url": "https://evil.example.com/v/abc"},  # host not allowed
        {"kind": "tour_3d", "url": "https://www.youtube.com/x"},  # wrong host for kind
        {"kind": "photo", "url": "https://www.youtube.com/x"},  # photos are uploads
    ):
        resp = await client.post(embeds_url, json=bad, headers=admin)
        assert resp.status_code == 422, bad

    # And the reverse: embed kinds cannot go through the upload endpoint.
    resp = await request_upload(client, admin, listing["id"], kind="video")
    assert resp.status_code == 422


# ---- management ----


async def test_cover_rules_and_position(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    first = await upload_and_confirm(client, admin, listing["id"], jpeg_bytes())
    second = await upload_and_confirm(client, admin, listing["id"], jpeg_bytes(300, 200))
    assert (first["position"], second["position"]) == (0, 1)

    # A pending upload can't be the cover.
    pending = (await request_upload(client, admin, listing["id"])).json()["media"]
    resp = await client.patch(
        f"/api/v1/portal/media/{pending['id']}", json={"isCover": True}, headers=admin
    )
    assert resp.status_code == 409

    resp = await client.patch(
        f"/api/v1/portal/media/{first['id']}", json={"isCover": True}, headers=admin
    )
    assert resp.status_code == 200 and resp.json()["isCover"] is True

    # Setting a new cover atomically unsets the old one.
    resp = await client.patch(
        f"/api/v1/portal/media/{second['id']}",
        json={"isCover": True, "altText": {"fr": "Salon"}},
        headers=admin,
    )
    assert resp.status_code == 200
    listed = (
        await client.get(f"/api/v1/portal/listings/{listing['id']}/media", headers=admin)
    ).json()
    covers = {m["id"]: m["isCover"] for m in listed}
    assert covers[second["id"]] is True and covers[first["id"]] is False


async def test_document_stays_private_with_presigned_download(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    doc = await upload_and_confirm(
        client, admin, listing["id"], PDF_BYTES, kind="doc", content_type="application/pdf"
    )
    assert doc["status"] == "ready"
    assert doc["variants"] == {}  # no image pipeline for PDFs

    resp = await client.get(f"/api/v1/portal/media/{doc['id']}/download", headers=admin)
    assert resp.status_code == 200
    url = resp.json()["downloadUrl"]
    assert "media-private-test" in url
    async with httpx.AsyncClient() as direct:
        got = await direct.get(url)
        assert got.status_code == 200
        assert got.content == PDF_BYTES
        # Without the signature the object is not reachable (private bucket).
        unsigned = await direct.get(url.split("?")[0])
        assert unsigned.status_code in (401, 403)


async def test_delete_removes_row_and_objects(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)
    media = await upload_and_confirm(client, admin, listing["id"], jpeg_bytes())
    variant_url = media["variants"]["thumb_jpeg"]["url"]
    original_url = (
        await client.get(f"/api/v1/portal/media/{media['id']}/download", headers=admin)
    ).json()["downloadUrl"]

    resp = await client.delete(f"/api/v1/portal/media/{media['id']}", headers=admin)
    assert resp.status_code == 204

    listed = (
        await client.get(f"/api/v1/portal/listings/{listing['id']}/media", headers=admin)
    ).json()
    assert media["id"] not in {m["id"] for m in listed}
    # The cleanup task (eager) removed both the public variants and the
    # private original — the still-valid presigned URL now 404s.
    async with httpx.AsyncClient() as direct:
        assert (await direct.get(variant_url)).status_code == 404
        assert (await direct.get(original_url)).status_code == 404


# ---- public site ----


async def test_public_listing_carries_cover_and_gallery(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    listing = await make_listing(client, admin)

    photo1 = await upload_and_confirm(client, admin, listing["id"], jpeg_bytes())
    photo2 = await upload_and_confirm(client, admin, listing["id"], jpeg_bytes(500, 400))
    await client.post(
        f"/api/v1/portal/listings/{listing['id']}/media/embeds",
        json={"kind": "video", "url": "https://vimeo.com/12345"},
        headers=admin,
    )
    await upload_and_confirm(
        client, admin, listing["id"], PDF_BYTES, kind="doc", content_type="application/pdf"
    )
    await client.patch(
        f"/api/v1/portal/media/{photo2['id']}",
        json={"isCover": True, "altText": {"fr": "Vue du salon", "en": "Living room"}},
        headers=admin,
    )
    assert (await transition(client, admin, listing["id"], "published")).status_code == 200

    page = await client.get("/api/v1/listings", headers={"Host": "agency-a.test"})
    assert page.status_code == 200, page.text
    item = page.json()["items"][0]
    assert item["cover"]["id"] == photo2["id"]
    assert item["media"] is None  # list responses carry the cover only

    detail = await client.get(
        f"/api/v1/listings/{listing['referenceCode']}?locale=fr",
        headers={"Host": "agency-a.test"},
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["cover"]["id"] == photo2["id"]
    assert body["cover"]["alt"] == "Vue du salon"
    # Photos + embed are public; the private document is not.
    kinds = sorted(m["kind"] for m in body["media"])
    assert kinds == ["photo", "photo", "video"]
    ids = {m["id"] for m in body["media"]}
    assert photo1["id"] in ids and photo2["id"] in ids


# ---- scoping & isolation ----


async def test_agent_scoping_and_tenant_isolation(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    tenant_a, admin_a = await tenant_and_login(
        client, platform_headers, create_tenant_user, Role.ADMIN
    )
    agent1 = await add_user(
        client, create_tenant_user, str(tenant_a["id"]), Role.AGENT, email="agent1@a.example.com"
    )
    agent2 = await add_user(
        client, create_tenant_user, str(tenant_a["id"]), Role.AGENT, email="agent2@a.example.com"
    )
    listing = await make_listing(client, agent1)
    media = (await request_upload(client, agent1, listing["id"])).json()["media"]

    # Another agent gets 404 for the whole media surface — no existence oracle.
    resp = await client.get(f"/api/v1/portal/listings/{listing['id']}/media", headers=agent2)
    assert resp.status_code == 404
    resp = await client.post(f"/api/v1/portal/media/{media['id']}/confirm", headers=agent2)
    assert resp.status_code == 404
    # Admin (tenant-wide) sees it.
    resp = await client.get(f"/api/v1/portal/listings/{listing['id']}/media", headers=admin_a)
    assert resp.status_code == 200

    # A whole other tenant: same 404, RLS + explicit tenant filter both apply.
    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain="agency-b.test"
    )
    admin_b = await add_user(
        client,
        create_tenant_user,
        str(tenant_b["id"]),
        Role.ADMIN,
        email="admin@b.example.com",
        host="agency-b.test",
    )
    resp = await client.get(f"/api/v1/portal/listings/{listing['id']}/media", headers=admin_b)
    assert resp.status_code == 404
    resp = await client.post(f"/api/v1/portal/media/{media['id']}/confirm", headers=admin_b)
    assert resp.status_code == 404
