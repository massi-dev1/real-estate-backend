"""Users module: self profile + tenant-admin user management."""

import uuid
from collections.abc import Awaitable, Callable

from httpx import AsyncClient

from app.core.permissions import Role
from tests.conftest import FIXTURE_PASSWORD
from tests.helpers import HOST_A, bearer, login_user, register_user
from tests.test_auth_flows import make_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]


async def admin_headers(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> dict[str, str]:
    tenant = await make_tenant(client, platform_headers)
    await create_tenant_user(str(tenant["id"]), "admin@agency-a.example.com", Role.ADMIN)
    resp = await login_user(client, HOST_A, "admin@agency-a.example.com", FIXTURE_PASSWORD)
    assert resp.status_code == 200, resp.text
    return {"Host": HOST_A, "Authorization": bearer(resp)}


async def test_profile_update(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    await make_tenant(client, platform_headers)
    resp = await register_user(client, HOST_A)
    headers = {"Host": HOST_A, "Authorization": bearer(resp)}

    patched = await client.patch(
        "/api/v1/users/me", json={"firstName": "Karim", "phone": "+213555000111"}, headers=headers
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["firstName"] == "Karim"
    assert patched.json()["phone"] == "+213555000111"

    # Unknown fields are rejected (extra="forbid").
    bad = await client.patch("/api/v1/users/me", json={"role": "admin"}, headers=headers)
    assert bad.status_code == 422


async def test_me_requires_auth(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    await make_tenant(client, platform_headers)
    assert (await client.get("/api/v1/users/me", headers={"Host": HOST_A})).status_code == 401


async def test_admin_creates_and_manages_users(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    headers = await admin_headers(client, platform_headers, create_tenant_user)

    created = await client.post(
        "/api/v1/users",
        json={
            "email": "agent@agency-a.example.com",
            "password": "Agent-Pass-123456",
            "role": "agent",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    assert created.json()["role"] == "agent"

    duplicate = await client.post(
        "/api/v1/users",
        json={
            "email": "agent@agency-a.example.com",
            "password": "Agent-Pass-123456",
            "role": "agent",
        },
        headers=headers,
    )
    assert duplicate.status_code == 409

    platform_role = await client.post(
        "/api/v1/users",
        json={
            "email": "x@agency-a.example.com",
            "password": "Some-Pass-123456",
            "role": "platform_admin",
        },
        headers=headers,
    )
    assert platform_role.status_code == 422

    listed = await client.get("/api/v1/users", headers=headers)
    assert listed.status_code == 200
    emails = {u["email"] for u in listed.json()["items"]}
    assert {"admin@agency-a.example.com", "agent@agency-a.example.com"} <= emails

    promoted = await client.patch(
        f"/api/v1/users/{agent_id}", json={"role": "team_lead"}, headers=headers
    )
    assert promoted.status_code == 200
    assert promoted.json()["role"] == "team_lead"

    # The new agent can log in; a disabled one cannot.
    agent_login = await login_user(
        client, HOST_A, "agent@agency-a.example.com", "Agent-Pass-123456"
    )
    assert agent_login.status_code == 200
    disabled = await client.patch(
        f"/api/v1/users/{agent_id}", json={"status": "disabled"}, headers=headers
    )
    assert disabled.status_code == 200
    assert (
        await login_user(client, HOST_A, "agent@agency-a.example.com", "Agent-Pass-123456")
    ).status_code == 401
    # ...and the access token the agent already held died with the disable.
    me = await client.get(
        "/api/v1/users/me", headers={"Host": HOST_A, "Authorization": bearer(agent_login)}
    )
    assert me.status_code == 401


async def test_admin_cannot_lock_themselves_out(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    headers = await admin_headers(client, platform_headers, create_tenant_user)
    me = await client.get("/api/v1/users/me", headers=headers)
    my_id = me.json()["id"]

    demote = await client.patch(f"/api/v1/users/{my_id}", json={"role": "agent"}, headers=headers)
    assert demote.status_code == 409
    delete = await client.delete(f"/api/v1/users/{my_id}", headers=headers)
    assert delete.status_code == 409


async def test_soft_delete_blocks_login_and_lookup(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    headers = await admin_headers(client, platform_headers, create_tenant_user)
    created = await client.post(
        "/api/v1/users",
        json={
            "email": "gone@agency-a.example.com",
            "password": "Gone-Pass-123456",
            "role": "seller",
        },
        headers=headers,
    )
    user_id = created.json()["id"]

    deleted = await client.delete(f"/api/v1/users/{user_id}", headers=headers)
    assert deleted.status_code == 204

    assert (await client.get(f"/api/v1/users/{user_id}", headers=headers)).status_code == 404
    assert (
        await login_user(client, HOST_A, "gone@agency-a.example.com", "Gone-Pass-123456")
    ).status_code == 401
