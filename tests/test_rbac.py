"""RBAC matrix (§7.2/§13): role-by-endpoint expected-status, table-driven,
plus platform-staff management and tenant/platform scope isolation."""

import uuid
from collections.abc import Awaitable, Callable

import pytest
from httpx import AsyncClient

from app.core.permissions import Role
from tests.conftest import FIXTURE_PASSWORD
from tests.helpers import HOST_A, bearer, login_user
from tests.test_auth_flows import make_tenant

CreateTenantUser = Callable[..., Awaitable[uuid.UUID]]


async def role_headers(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    role: Role,
) -> dict[str, str]:
    tenant = await make_tenant(client, platform_headers)
    email = f"{role.value}@agency-a.example.com"
    await create_tenant_user(str(tenant["id"]), email, role)
    resp = await login_user(client, HOST_A, email, FIXTURE_PASSWORD)
    assert resp.status_code == 200, resp.text
    return {"Host": HOST_A, "Authorization": bearer(resp)}


# (role, method, path, body, expected) — the tenant-side permission matrix.
TENANT_MATRIX = [
    (Role.ADMIN, "GET", "/api/v1/users", None, 200),
    (Role.AGENT, "GET", "/api/v1/users", None, 403),
    (Role.BUYER_RENTER, "GET", "/api/v1/users", None, 403),
    (Role.MARKETING, "GET", "/api/v1/users", None, 403),
    (
        Role.TEAM_LEAD,
        "POST",
        "/api/v1/users",
        {"email": "n@a.example.com", "password": "Some-Pass-123456", "role": "agent"},
        403,
    ),
    (
        Role.ADMIN,
        "POST",
        "/api/v1/users",
        {"email": "n@a.example.com", "password": "Some-Pass-123456", "role": "agent"},
        201,
    ),
    (Role.SELLER, "GET", "/api/v1/users/me", None, 200),
]


@pytest.mark.parametrize(("role", "method", "path", "body", "expected"), TENANT_MATRIX)
async def test_tenant_role_matrix(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
    role: Role,
    method: str,
    path: str,
    body: dict[str, str] | None,
    expected: int,
) -> None:
    headers = await role_headers(client, platform_headers, create_tenant_user, role)
    resp = await client.request(method, path, json=body, headers=headers)
    assert resp.status_code == expected, f"{role} {method} {path}: {resp.text}"


async def make_staff_headers(
    client: AsyncClient, platform_headers: dict[str, str], role: Role
) -> dict[str, str]:
    email = f"{role.value}@platform.example.com"
    created = await client.post(
        "/api/v1/platform/staff",
        json={"email": email, "password": "Staff-Pass-123456", "role": role.value},
        headers=platform_headers,
    )
    assert created.status_code == 201, created.text
    resp = await client.post(
        "/api/v1/platform/auth/login", json={"email": email, "password": "Staff-Pass-123456"}
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": bearer(resp)}


async def test_platform_support_is_read_only(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    support = await make_staff_headers(client, platform_headers, Role.PLATFORM_SUPPORT)

    assert (await client.get("/api/v1/platform/tenants", headers=support)).status_code == 200
    create = await client.post(
        "/api/v1/platform/tenants",
        json={"name": "Nope", "slug": "nope", "domain": "nope.test"},
        headers=support,
    )
    assert create.status_code == 403
    staff = await client.post(
        "/api/v1/platform/staff",
        json={
            "email": "s2@platform.example.com",
            "password": "Staff-Pass-123456",
            "role": "platform_support",
        },
        headers=support,
    )
    assert staff.status_code == 403


async def test_platform_admin_manages_staff(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_staff_headers(client, platform_headers, Role.PLATFORM_SUPPORT)
    listed = await client.get("/api/v1/platform/staff", headers=platform_headers)
    assert listed.status_code == 200
    emails = {u["email"] for u in listed.json()["items"]}
    assert "platform_support@platform.example.com" in emails


async def test_tenant_token_rejected_on_platform_routes(
    client: AsyncClient,
    platform_headers: dict[str, str],
    create_tenant_user: CreateTenantUser,
) -> None:
    admin = await role_headers(client, platform_headers, create_tenant_user, Role.ADMIN)
    resp = await client.get(
        "/api/v1/platform/tenants", headers={"Authorization": admin["Authorization"]}
    )
    assert resp.status_code == 401


async def test_platform_token_rejected_on_tenant_routes(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    resp = await client.get(
        "/api/v1/users/me",
        headers={"Host": HOST_A, "Authorization": platform_headers["Authorization"]},
    )
    assert resp.status_code == 401
