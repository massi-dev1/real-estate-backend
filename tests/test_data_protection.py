"""Data-protection primitives (§10.7, §9): AES-GCM field encryption and the
Idempotency-Key header facility, both reusable across future modules."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import Column, MetaData, String, Table, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.crypto import EncryptedString, FieldCipher, get_field_cipher
from app.core.permissions import Role
from tests.helpers import HOST_A, HOST_B
from tests.test_appointments import (
    PORTAL_APPOINTMENTS,
    SLUG,
    booking_body,
    setup_published_agent,
    slot_at,
    tomorrow,
    weekly_rules,
)
from tests.test_leads import CAPTURE_URL, capture_body
from tests.test_listings import CreateTenantUser, add_user, tenant_and_login
from tests.test_tenants_platform_api import create_tenant

# ---- field encryption (§10.7) ----


def test_encrypt_decrypt_round_trip() -> None:
    cipher = get_field_cipher(get_settings())
    token = cipher.encrypt("a-totp-seed")
    assert cipher.decrypt(token) == "a-totp-seed"


def test_ciphertext_differs_from_plaintext_and_carries_key_id() -> None:
    cipher = get_field_cipher(get_settings())
    plaintext = "JBSWY3DPEHPK3PXP"
    token = cipher.encrypt(plaintext)
    assert plaintext not in token
    key_id, _, _ = token.partition(":")
    assert key_id == get_settings().field_encryption_key_id


def test_encrypting_the_same_value_twice_yields_different_ciphertext() -> None:
    # Random nonce per call: two ciphertexts of the same plaintext must never
    # be comparable, which is also what keeps identical secrets from leaking
    # via equal-ciphertext correlation.
    cipher = get_field_cipher(get_settings())
    a = cipher.encrypt("same-value")
    b = cipher.encrypt("same-value")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == "same-value"


def test_tampered_ciphertext_fails_to_decrypt() -> None:
    cipher = get_field_cipher(get_settings())
    token = cipher.encrypt("secret")
    key_id, _, payload = token.partition(":")
    tampered = f"{key_id}:{payload[:-4]}AAAA"
    with pytest.raises(Exception):  # noqa: B017 - AEAD raises cryptography's InvalidTag
        cipher.decrypt(tampered)


def test_unknown_key_id_fails_to_decrypt() -> None:
    cipher = get_field_cipher(get_settings())
    with pytest.raises(ValueError, match="Unknown field-encryption key id"):
        cipher.decrypt("no-such-key:deadbeef")


def test_key_rotation_keyring_decrypts_old_and_new() -> None:
    """A ciphertext minted under a retired key id still decrypts once that
    id+key is carried forward in ``field_encryption_keys`` (§10.7 rotation)."""
    settings = get_settings()
    old_cipher = FieldCipher(settings)
    old_token = old_cipher.encrypt("pre-rotation-secret")

    retired = f"{settings.field_encryption_key_id}={settings.field_encryption_key}"
    rotated = settings.model_copy(
        update={
            "field_encryption_key_id": "v2",
            "field_encryption_key": "rotated-key-0123456789abcdef0123456789ab",
            "field_encryption_keys": retired,
        }
    )
    new_cipher = FieldCipher(rotated)
    assert new_cipher.decrypt(old_token) == "pre-rotation-secret"
    new_token = new_cipher.encrypt("post-rotation-secret")
    assert new_token.startswith("v2:")
    assert new_cipher.decrypt(new_token) == "post-rotation-secret"


_metadata = MetaData()
_secrets_probe = Table(
    "encrypted_field_probe",
    _metadata,
    Column("id", String(36), primary_key=True),
    Column("secret", EncryptedString(255)),
)


@pytest.fixture
async def secrets_probe() -> AsyncIterator[None]:
    ddl_engine = create_async_engine(os.environ["DATABASE_DDL_URL"])
    async with ddl_engine.begin() as conn:
        await conn.run_sync(_metadata.create_all, tables=[_secrets_probe])
    try:
        yield
    finally:
        async with ddl_engine.begin() as conn:
            await conn.run_sync(_metadata.drop_all, tables=[_secrets_probe])
        await ddl_engine.dispose()


async def test_encrypted_string_column_stores_ciphertext_not_plaintext(
    app: FastAPI, secrets_probe: None
) -> None:
    row_id = str(uuid.uuid4())
    plaintext = "JBSWY3DPEHPK3PXP"
    async with app.state.session_factory() as session, session.begin():
        await session.execute(_secrets_probe.insert().values(id=row_id, secret=plaintext))

    # Raw read bypasses the TypeDecorator, so this sees exactly what Postgres
    # stored on disk.
    async with app.state.session_factory() as session:
        raw = (
            await session.execute(
                text("SELECT secret FROM encrypted_field_probe WHERE id = :id"), {"id": row_id}
            )
        ).scalar_one()
    assert plaintext not in raw
    assert raw.startswith(f"{get_settings().field_encryption_key_id}:")

    # The ORM path decrypts transparently.
    async with app.state.session_factory() as session:
        stmt = select(_secrets_probe.c.secret).where(_secrets_probe.c.id == row_id)
        decrypted = (await session.execute(stmt)).scalar_one()
    assert decrypted == plaintext


# ---- Idempotency-Key (§9) ----


async def _capture_with_key(
    client: AsyncClient, key: str | None, *, email: str = "idem@example.com", host: str = HOST_A
) -> Any:
    headers = {"Host": host}
    if key is not None:
        headers["Idempotency-Key"] = key
    return await client.post(CAPTURE_URL, json=capture_body(email=email), headers=headers)


async def test_repeated_idempotency_key_returns_cached_response_and_one_row(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    key = str(uuid.uuid4())

    first = await _capture_with_key(client, key)
    assert first.status_code == 201, first.text
    second = await _capture_with_key(client, key)
    assert second.status_code == 201, second.text
    # Byte-identical: the replay is the cached response, not a fresh insert.
    assert first.json() == second.json()

    listed = await client.get("/api/v1/portal/leads", headers=admin)
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 1


async def test_missing_idempotency_key_executes_every_request(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    first = await _capture_with_key(client, None, email="a@example.com")
    second = await _capture_with_key(client, None, email="b@example.com")
    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["id"] != second.json()["id"]

    listed = await client.get("/api/v1/portal/leads", headers=admin)
    assert len(listed.json()["items"]) == 2


async def test_concurrent_duplicate_idempotency_key_gets_409(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    key = str(uuid.uuid4())

    results = await asyncio.gather(
        _capture_with_key(client, key),
        _capture_with_key(client, key),
    )
    statuses = sorted(r.status_code for r in results)
    assert statuses == [201, 409], [r.text for r in results]
    conflict = next(r for r in results if r.status_code == 409)
    assert conflict.json()["type"].endswith("idempotency-key-in-flight")

    listed = await client.get("/api/v1/portal/leads", headers=admin)
    assert len(listed.json()["items"]) == 1


def test_idempotency_replay_headers_preserve_custom_and_strip_regenerated() -> None:
    """A cached replay must carry the original response's non-regenerated
    headers (e.g. a Location/Set-Cookie a future wired endpoint might set),
    while content-length/content-type — recomputed fresh by Response.
    init_headers from the replayed body/media_type — aren't duplicated."""
    from app.core.idempotency import _replay_headers

    stored = {
        "content-length": "123",
        "content-type": "application/json",
        "x-custom-header": "keep-me",
        "set-cookie": "session=abc",
    }
    replayed = _replay_headers(stored)
    assert replayed is not None
    assert "content-length" not in replayed
    assert "content-type" not in replayed
    assert replayed["x-custom-header"] == "keep-me"
    assert replayed["set-cookie"] == "session=abc"
    assert _replay_headers(None) is None
    assert _replay_headers({}) is None


async def test_idempotency_lock_renewed_for_slow_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A handler slower than one lock window must not lose its claim mid-
    flight — the renewal loop should have extended the TTL at least once
    before the handler returns."""
    from app.core import idempotency as idem_module

    class _FakeRedis:
        def __init__(self) -> None:
            self.expire_calls = 0

        async def expire(self, key: str, ttl: int) -> bool:
            self.expire_calls += 1
            return True

    monkeypatch.setattr(idem_module, "_LOCK_SECONDS", 1)
    redis = _FakeRedis()
    task = asyncio.ensure_future(
        idem_module._renew_lock(redis, "idempotency:test:key", "/test")  # type: ignore[arg-type]
    )
    await asyncio.sleep(1.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert redis.expire_calls >= 1


async def test_idempotency_key_scoped_per_tenant(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """The same key from two different tenants must not collide — each
    creates its own lead, mirroring the cache key's tenant component."""
    _, admin_a = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    tenant_b = await create_tenant(
        client, platform_headers, name="Agency B", slug="agency-b", domain=HOST_B
    )
    admin_b = await add_user(
        client, create_tenant_user, str(tenant_b["id"]), Role.ADMIN, host=HOST_B
    )

    key = str(uuid.uuid4())
    resp_a = await _capture_with_key(client, key, host=HOST_A)
    resp_b = await _capture_with_key(client, key, host=HOST_B)
    assert resp_a.status_code == 201
    assert resp_b.status_code == 201
    assert resp_a.json()["id"] != resp_b.json()["id"]

    listed_a = await client.get("/api/v1/portal/leads", headers=admin_a)
    listed_b = await client.get("/api/v1/portal/leads", headers=admin_b)
    assert len(listed_a.json()["items"]) == 1
    assert len(listed_b.json()["items"]) == 1


async def test_idempotency_degrades_open_when_redis_down(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consistent with the rate limiter's stance: a Redis outage must not take
    these endpoints down — the request just executes without idempotency
    protection instead of failing."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)

    class _BrokenRedis:
        async def get(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis unavailable")

        async def set(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis unavailable")

        async def delete(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis unavailable")

    real_redis = app.state.redis
    app.state.redis = _BrokenRedis()
    try:
        resp = await _capture_with_key(client, str(uuid.uuid4()))
        assert resp.status_code == 201, resp.text
    finally:
        app.state.redis = real_redis

    listed = await client.get("/api/v1/portal/leads", headers=admin)
    assert len(listed.json()["items"]) == 1


async def test_idempotency_key_not_replayed_after_ttl_window_key_reused(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    app: FastAPI,
) -> None:
    """A cache entry that has already expired in Redis behaves exactly like a
    fresh key — proves replay is driven by the TTL, not the key string alone."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    key = str(uuid.uuid4())

    first = await _capture_with_key(client, key, email="ttl@example.com")
    assert first.status_code == 201

    # Simulate TTL expiry by flushing just this key rather than waiting 24h.
    async for redis_key in app.state.redis.scan_iter(match=f"*{key}"):
        await app.state.redis.delete(redis_key)

    second = await _capture_with_key(client, key, email="ttl@example.com")
    assert second.status_code == 201
    assert second.json()["id"] != first.json()["id"]

    listed = await client.get("/api/v1/portal/leads", headers=admin)
    assert len(listed.json()["items"]) == 2


async def test_idempotency_key_ignored_on_get(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """Only routes wired onto IdempotentRoute honour the header — an ordinary
    GET must not be affected by a stray Idempotency-Key."""
    _, admin = await tenant_and_login(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.get(
        "/api/v1/portal/leads",
        headers={**admin, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 200


async def test_repeated_idempotency_key_on_tour_booking_creates_one_appointment(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    """The booking POST is the second named wiring point (§9): a retried
    tour-booking request must return the same appointment, not a 409 from the
    service's own advisory-lock double-booking guard for a *distinct* slot."""
    _, admin, _agent, _agent_id, _profile = await setup_published_agent(
        client, platform_headers, create_tenant_user, rules=weekly_rules()
    )

    day = tomorrow()
    start_at = slot_at(day, 9)
    key = str(uuid.uuid4())
    headers = {"Host": HOST_A, "Idempotency-Key": key}
    body = booking_body(start_at, email="idem-tour@example.com")

    first = await client.post(f"/api/v1/agents/{SLUG}/appointments", json=body, headers=headers)
    assert first.status_code == 201, first.text
    second = await client.post(f"/api/v1/agents/{SLUG}/appointments", json=body, headers=headers)
    assert second.status_code == 201, second.text
    assert first.json() == second.json()

    listed = await client.get(PORTAL_APPOINTMENTS, headers=admin)
    assert len(listed.json()["items"]) == 1


async def test_repeated_idempotency_key_on_billing_checkout_returns_same_session(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """The third named wiring point (§9): a retried checkout call must not
    open a second billing-provider session for the same tenant."""
    tenant = await create_tenant(client, platform_headers)
    key = str(uuid.uuid4())
    body = {"plan": "growth", "customerEmail": "billing@agency-a.example.com"}

    first = await client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/checkout",
        json=body,
        headers={**platform_headers, "Idempotency-Key": key},
    )
    assert first.status_code == 201, first.text
    second = await client.post(
        f"/api/v1/platform/tenants/{tenant['id']}/checkout",
        json=body,
        headers={**platform_headers, "Idempotency-Key": key},
    )
    assert second.status_code == 201, second.text
    assert first.json() == second.json()
