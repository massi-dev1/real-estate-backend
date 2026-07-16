"""Auth flows (§7.1): register/login, refresh rotation + reuse detection,
logout, password reset, email verification, cross-tenant token rejection."""

from httpx import AsyncClient

from tests.helpers import (
    HOST_A,
    HOST_B,
    bearer,
    login_user,
    mailpit_code,
    refresh_cookie,
    register_user,
    use_refresh_cookie,
)
from tests.test_tenants_platform_api import create_tenant


async def make_tenant(
    client: AsyncClient,
    platform_headers: dict[str, str],
    *,
    slug: str = "agency-a",
    domain: str = HOST_A,
) -> dict[str, object]:
    return await create_tenant(
        client, platform_headers, name=slug.title(), slug=slug, domain=domain
    )


async def test_register_and_me(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    await make_tenant(client, platform_headers)

    resp = await register_user(client, HOST_A, first_name="Nadia")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["tokenType"] == "bearer"
    assert body["expiresIn"] == 900
    assert body["user"]["email"] == "buyer@example.com"
    assert body["user"]["role"] == "buyer_renter"
    assert body["user"]["emailVerifiedAt"] is None
    assert refresh_cookie(resp)

    me = await client.get(
        "/api/v1/users/me", headers={"Host": HOST_A, "Authorization": bearer(resp)}
    )
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "buyer@example.com"
    assert me.json()["firstName"] == "Nadia"


async def test_register_duplicate_email_conflicts(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    assert (await register_user(client, HOST_A)).status_code == 201
    assert (await register_user(client, HOST_A)).status_code == 409


async def test_register_privileged_role_rejected(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    resp = await register_user(client, HOST_A, role="admin")
    assert resp.status_code == 422


async def test_login_is_generic_on_failure(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    await register_user(client, HOST_A)

    wrong_password = await login_user(client, HOST_A, "buyer@example.com", "wrong-password-1")
    unknown_email = await login_user(client, HOST_A, "ghost@example.com", "wrong-password-1")
    assert wrong_password.status_code == unknown_email.status_code == 401
    # Identical problem bodies — no user-enumeration signal (§7.1).
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


async def test_token_is_useless_on_another_tenant(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers, slug="agency-a", domain=HOST_A)
    await make_tenant(client, platform_headers, slug="agency-b", domain=HOST_B)
    resp = await register_user(client, HOST_A)

    me_on_b = await client.get(
        "/api/v1/users/me", headers={"Host": HOST_B, "Authorization": bearer(resp)}
    )
    assert me_on_b.status_code == 401


async def test_refresh_rotation_and_reuse_detection(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    first = await register_user(client, HOST_A)
    token_1 = refresh_cookie(first)

    use_refresh_cookie(client, token_1)
    rotated = await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})
    assert rotated.status_code == 200, rotated.text
    token_2 = refresh_cookie(rotated)
    assert token_2 != token_1

    # Replaying the rotated-away token is the theft signal...
    use_refresh_cookie(client, token_1)
    reuse = await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})
    assert reuse.status_code == 401

    # ...which must revoke the whole family, including the newest token.
    use_refresh_cookie(client, token_2)
    after_reuse = await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})
    assert after_reuse.status_code == 401


async def test_refresh_requires_cookie_and_right_tenant(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers, slug="agency-a", domain=HOST_A)
    await make_tenant(client, platform_headers, slug="agency-b", domain=HOST_B)
    resp = await register_user(client, HOST_A)

    client.cookies.clear()
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})).status_code == 401

    # A tenant-A refresh token presented on tenant B's domain finds nothing
    # (identity RLS + explicit tenant scope).
    use_refresh_cookie(client, refresh_cookie(resp))
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_B})).status_code == 401


async def test_logout_revokes_session_and_access_token(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    resp = await register_user(client, HOST_A)
    access = bearer(resp)
    token = refresh_cookie(resp)

    use_refresh_cookie(client, token)
    out = await client.post(
        "/api/v1/auth/logout", headers={"Host": HOST_A, "Authorization": access}
    )
    assert out.status_code == 204

    use_refresh_cookie(client, token)
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})).status_code == 401
    # The access jti is denylisted for its remaining lifetime.
    me = await client.get("/api/v1/users/me", headers={"Host": HOST_A, "Authorization": access})
    assert me.status_code == 401


async def test_logout_all_revokes_every_session(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    await register_user(client, HOST_A)
    second = await login_user(client, HOST_A, "buyer@example.com", "Buyer-Pass-123456")
    third = await login_user(client, HOST_A, "buyer@example.com", "Buyer-Pass-123456")

    out = await client.post(
        "/api/v1/auth/logout-all", headers={"Host": HOST_A, "Authorization": bearer(third)}
    )
    assert out.status_code == 204

    for resp in (second, third):
        use_refresh_cookie(client, refresh_cookie(resp))
        refreshed = await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})
        assert refreshed.status_code == 401

    # The other device's still-valid access token dies too, not just refresh.
    me = await client.get(
        "/api/v1/users/me", headers={"Host": HOST_A, "Authorization": bearer(second)}
    )
    assert me.status_code == 401


async def test_password_reset_flow(client: AsyncClient, platform_headers: dict[str, str]) -> None:
    await make_tenant(client, platform_headers)
    email = "reset-me@example.com"
    first = await register_user(client, HOST_A, email=email)

    # Unknown emails get the same 202 — no enumeration.
    unknown = await client.post(
        "/api/v1/auth/password/forgot",
        json={"email": "ghost@example.com"},
        headers={"Host": HOST_A},
    )
    assert unknown.status_code == 202

    resp = await client.post(
        "/api/v1/auth/password/forgot", json={"email": email}, headers={"Host": HOST_A}
    )
    assert resp.status_code == 202
    code = await mailpit_code(email, "Reset")

    resp = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": code, "newPassword": "New-Pass-654321"},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 204, resp.text

    assert (await login_user(client, HOST_A, email, "Buyer-Pass-123456")).status_code == 401
    assert (await login_user(client, HOST_A, email, "New-Pass-654321")).status_code == 200

    # Single use: the same code must not work twice.
    resp = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": code, "newPassword": "Third-Pass-999999"},
        headers={"Host": HOST_A},
    )
    assert resp.status_code == 401

    # All pre-reset sessions were revoked — refresh and live access tokens.
    use_refresh_cookie(client, refresh_cookie(first))
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})).status_code == 401
    me = await client.get(
        "/api/v1/users/me", headers={"Host": HOST_A, "Authorization": bearer(first)}
    )
    assert me.status_code == 401


async def test_email_verification_flow(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    email = "verify-me@example.com"
    resp = await register_user(client, HOST_A, email=email)
    access = bearer(resp)

    code = await mailpit_code(email, "Verify")
    verified = await client.post(
        "/api/v1/auth/verify-email", json={"token": code}, headers={"Host": HOST_A}
    )
    assert verified.status_code == 204, verified.text

    me = await client.get("/api/v1/users/me", headers={"Host": HOST_A, "Authorization": access})
    assert me.json()["emailVerifiedAt"] is not None

    # Single use.
    again = await client.post(
        "/api/v1/auth/verify-email", json={"token": code}, headers={"Host": HOST_A}
    )
    assert again.status_code == 401
