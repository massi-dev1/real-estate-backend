"""Part 29 — auth hardening (§7.1, §10.3).

Account lockout with backoff, breached-password rejection (HIBP mocked — the
suite never reaches a third party), TOTP enrol → verify → enforced login with
the secret unreadable in the database, the OAuth seam reporting "not
configured" without credentials, and the session list + per-session revoke.
"""

from typing import Any

import pyotp
import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import text

from app.core import security
from app.core.config import get_settings
from app.core.lockout import LoginLockout, _backoff_seconds
from app.integrations.breach import hibp
from app.modules.auth import service as auth_service
from tests.helpers import HOST_A, bearer, login_user, refresh_cookie, register_user
from tests.test_auth_flows import make_tenant

PASSWORD = "Buyer-Pass-123456"
EMAIL = "hardening@example.com"


def _hibp_enabled_settings() -> Any:
    """Conftest disables HIBP suite-wide (no network in tests); the checker's
    own unit tests need it on, with the transport faked."""
    return get_settings().model_copy(update={"hibp_enabled": True})


class _FakeBreachChecker:
    """Stands in for HIBP so no test ever crosses the network."""

    def __init__(self, breached: set[str] | None = None) -> None:
        self.breached = breached or set()
        self.calls: list[str] = []

    async def is_breached(self, password: str) -> bool:
        self.calls.append(password)
        return password in self.breached


@pytest.fixture
def fake_breach(monkeypatch: pytest.MonkeyPatch) -> _FakeBreachChecker:
    """Patch the checker at the name the auth service binds it under."""
    checker = _FakeBreachChecker()
    monkeypatch.setattr(auth_service, "build_breach_checker", lambda settings: checker)
    return checker


# ---- account lockout (§7.1) ----


async def test_lockout_after_repeated_failures_then_unlocks(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    await make_tenant(client, platform_headers)
    await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)

    settings = get_settings()
    for _ in range(settings.login_max_failed_attempts):
        resp = await login_user(client, HOST_A, EMAIL, "wrong-password-1")
        assert resp.status_code == 401

    # Past the threshold the *correct* password is refused too — and with the
    # same generic body, so a locked account is not distinguishable (§7.1).
    locked = await login_user(client, HOST_A, EMAIL, PASSWORD)
    assert locked.status_code == 401
    wrong = await login_user(client, HOST_A, EMAIL, "wrong-password-1")
    assert locked.json()["detail"] == wrong.json()["detail"]

    # Expiring the backoff key is what a passed lockout window looks like.
    keys = [k async for k in app.state.redis.scan_iter(match="auth:lockout:*:locked")]
    assert keys, "no lockout key was set"
    await app.state.redis.delete(*keys)

    assert (await login_user(client, HOST_A, EMAIL, PASSWORD)).status_code == 200


async def test_successful_login_resets_the_failure_counter(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)

    settings = get_settings()
    for _ in range(settings.login_max_failed_attempts - 1):
        assert (await login_user(client, HOST_A, EMAIL, "wrong-password-1")).status_code == 401

    assert (await login_user(client, HOST_A, EMAIL, PASSWORD)).status_code == 200

    # The counter is cleared, so the same number of slips is survivable again —
    # a person who mistypes weekly must never accumulate into a lockout.
    for _ in range(settings.login_max_failed_attempts - 1):
        assert (await login_user(client, HOST_A, EMAIL, "wrong-password-1")).status_code == 401
    assert (await login_user(client, HOST_A, EMAIL, PASSWORD)).status_code == 200


async def test_account_lockout_is_scoped_per_tenant(app: FastAPI) -> None:
    """The account counter is keyed on tenant + email: locking an address at
    agency A must not lock the same address at agency B.

    Exercised at the unit level rather than through two HTTP logins, because
    the *per-IP* counter is deliberately shared across tenants — five failures
    from one host trip it regardless of which agency was targeted, which is the
    whole point of having a second key. Driving this through the API would
    therefore only ever prove the IP lock fired.
    """
    lockout = LoginLockout(app.state.redis, get_settings())
    tenant_a, tenant_b, ip = "tenant-a", "tenant-b", "10.0.0.1"

    for _ in range(get_settings().login_max_failed_attempts):
        await lockout.record_failure(tenant_a, EMAIL, ip)

    assert await lockout.is_locked(tenant_a, EMAIL, ip) is True
    # Same address at another agency, from a *different* source (so the shared
    # per-IP lock is out of the picture): untouched.
    assert await lockout.is_locked(tenant_b, EMAIL, "10.0.0.2") is False
    # And a different account at the same agency is untouched too.
    assert await lockout.is_locked(tenant_a, "someone-else@example.com", "10.0.0.2") is False


async def test_ip_lockout_stops_spraying_many_accounts(app: FastAPI) -> None:
    """The second key: one source failing against *different* accounts, each
    of which alone stays under its own threshold, is still stopped once it
    crosses the (much larger) per-IP budget."""
    lockout = LoginLockout(app.state.redis, get_settings())
    ip = "10.0.0.9"

    for n in range(get_settings().login_ip_max_failed_attempts):
        await lockout.record_failure("tenant-a", f"victim-{n}@example.com", ip)

    # No single account crossed its threshold...
    assert await lockout.is_locked("tenant-a", "victim-0@example.com", "10.0.0.8") is False
    # ...but the source did.
    assert await lockout.is_locked("tenant-a", "fresh@example.com", ip) is True


async def test_ip_budget_is_larger_than_the_account_budget(app: FastAPI) -> None:
    """A shared egress (corporate NAT, mobile CGNAT) must not be locked out by
    one client failing at the per-account rate: the IP key needs a far bigger
    budget, so an account-threshold burst from one IP leaves it unlocked."""
    settings = get_settings()
    assert settings.login_ip_max_failed_attempts > settings.login_max_failed_attempts
    lockout = LoginLockout(app.state.redis, settings)
    ip = "10.0.0.77"

    # One client fails exactly the per-account budget against distinct accounts
    # (so no single account locks) — the IP must still be open.
    for n in range(settings.login_max_failed_attempts):
        await lockout.record_failure("tenant-a", f"shared-{n}@example.com", ip)
    assert await lockout.is_locked("tenant-a", "legit@example.com", ip) is False


def test_backoff_doubles_and_is_capped() -> None:
    settings = get_settings()
    base, threshold = settings.login_lockout_base_seconds, settings.login_max_failed_attempts

    assert _backoff_seconds(threshold, threshold, settings) == base
    assert _backoff_seconds(threshold + 1, threshold, settings) == base * 2
    assert _backoff_seconds(threshold + 2, threshold, settings) == base * 4
    # A determined attacker must not be able to lock a real user out for days.
    capped = _backoff_seconds(threshold + 50, threshold, settings)
    assert capped == settings.login_lockout_max_seconds


async def test_lockout_degrades_open_when_redis_is_down(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI, monkeypatch: Any
) -> None:
    await make_tenant(client, platform_headers)
    await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)

    class _BrokenRedis:
        def __getattr__(self, name: str) -> Any:
            raise ConnectionError("redis is down")

    monkeypatch.setattr(app.state, "redis", _BrokenRedis())
    # Login still works: an outage must not lock everyone out of a live app.
    assert (await login_user(client, HOST_A, EMAIL, PASSWORD)).status_code == 200


# ---- breached passwords (§10.3) ----


async def test_breached_password_rejected_on_register(
    client: AsyncClient, platform_headers: dict[str, str], fake_breach: _FakeBreachChecker
) -> None:
    await make_tenant(client, platform_headers)
    fake_breach.breached.add("Password-12345678")

    resp = await register_user(client, HOST_A, email=EMAIL, password="Password-12345678")
    assert resp.status_code == 422, resp.text
    assert resp.json()["type"].endswith("breached-password")

    # A password not in the corpus is accepted through the same code path.
    assert (await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)).status_code == 201


async def test_breached_password_rejected_on_reset(
    client: AsyncClient, platform_headers: dict[str, str], fake_breach: _FakeBreachChecker
) -> None:
    from tests.helpers import mailpit_code

    await make_tenant(client, platform_headers)
    await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    await client.post(
        "/api/v1/auth/password/forgot", json={"email": EMAIL}, headers={"Host": HOST_A}
    )
    code = await mailpit_code(EMAIL, "Reset")
    fake_breach.breached.add("Breached-Pass-99")

    rejected = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": code, "newPassword": "Breached-Pass-99"},
        headers={"Host": HOST_A},
    )
    assert rejected.status_code == 422

    # The rejection must not have burned the single-use reset code, or a typo
    # in the *new* password would lock the person out of their own recovery.
    accepted = await client.post(
        "/api/v1/auth/password/reset",
        json={"token": code, "newPassword": "Fresh-Pass-654321"},
        headers={"Host": HOST_A},
    )
    assert accepted.status_code == 204, accepted.text


async def test_breach_check_fails_open_on_upstream_error(
    client: AsyncClient, platform_headers: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A third party being down must never block a signup (§10.3)."""
    await make_tenant(client, platform_headers)

    async def _explode(self: Any, *args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("hibp unreachable")

    monkeypatch.setattr("httpx.AsyncClient.get", _explode)
    checker = hibp.BreachChecker(_hibp_enabled_settings())
    monkeypatch.setattr(auth_service, "build_breach_checker", lambda settings: checker)

    resp = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    assert resp.status_code == 201, resp.text


async def test_breach_checker_parses_a_hibp_range_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unit-level: the k-anonymity match is done locally against the suffix."""
    import hashlib

    import httpx

    password = "correct horse battery staple"
    digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()
    prefix, suffix = digest[:5], digest[5:]
    sent: dict[str, str] = {}

    async def _fake_get(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        sent["url"] = url
        body = f"0000000000000000000000000000000000:0\n{suffix}:42\n"
        return httpx.Response(200, text=body, request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.AsyncClient.get", _fake_get)
    checker = hibp.BreachChecker(_hibp_enabled_settings())
    assert await checker.is_breached(password) is True
    # Only the 5-char prefix ever leaves the process.
    assert sent["url"].endswith(f"/{prefix}")
    assert suffix not in sent["url"]
    assert password not in sent["url"]

    # A padding row (count 0) is not a hit.
    async def _padding_only(self: Any, url: str, **kwargs: Any) -> httpx.Response:
        return httpx.Response(200, text=f"{suffix}:0\n", request=httpx.Request("GET", url))

    monkeypatch.setattr("httpx.AsyncClient.get", _padding_only)
    assert await checker.is_breached(password) is False


# ---- MFA / TOTP (§7.1) ----


async def _enrol_mfa(client: AsyncClient, access: str, app: FastAPI) -> str:
    enrol = await client.post(
        "/api/v1/auth/mfa/enrol", headers={"Host": HOST_A, "Authorization": access}
    )
    assert enrol.status_code == 201, enrol.text
    secret = enrol.json()["secret"]
    assert enrol.json()["provisioningUri"].startswith("otpauth://totp/")

    confirm = await client.post(
        "/api/v1/auth/mfa/enrol/confirm",
        json={"code": pyotp.TOTP(secret).now()},
        headers={"Host": HOST_A, "Authorization": access},
    )
    assert confirm.status_code == 204, confirm.text
    return str(secret)


async def test_mfa_enrol_verify_and_enforced_login(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    access = bearer(registered)

    status_before = await client.get(
        "/api/v1/auth/mfa/status", headers={"Host": HOST_A, "Authorization": access}
    )
    assert status_before.json() == {"enabled": False, "enrolledAt": None}

    secret = await _enrol_mfa(client, access, app)

    status_after = await client.get(
        "/api/v1/auth/mfa/status", headers={"Host": HOST_A, "Authorization": access}
    )
    assert status_after.json()["enabled"] is True
    assert status_after.json()["enrolledAt"] is not None

    # A password-only login now yields a challenge, not a session.
    challenge = await login_user(client, HOST_A, EMAIL, PASSWORD)
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    assert body["mfaRequired"] is True
    assert "accessToken" not in body
    assert challenge.cookies.get("refresh_token") is None

    verified = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfaToken": body["mfaToken"], "code": pyotp.TOTP(secret).now()},
        headers={"Host": HOST_A},
    )
    assert verified.status_code == 200, verified.text
    assert verified.json()["accessToken"]
    assert verified.json()["user"]["mfaEnabled"] is True
    assert refresh_cookie(verified)


async def test_mfa_ticket_is_single_use_and_wrong_code_burns_it(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    secret = await _enrol_mfa(client, bearer(registered), app)

    challenge = await login_user(client, HOST_A, EMAIL, PASSWORD)
    token = challenge.json()["mfaToken"]

    bad = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfaToken": token, "code": "000000"},
        headers={"Host": HOST_A},
    )
    assert bad.status_code == 401

    # The ticket was consumed before the code was checked: one guess per
    # ticket, so a five-minute window is not a million free attempts.
    replay = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfaToken": token, "code": pyotp.TOTP(secret).now()},
        headers={"Host": HOST_A},
    )
    assert replay.status_code == 401


async def test_mfa_ticket_from_another_tenant_is_refused(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    from tests.helpers import HOST_B

    await make_tenant(client, platform_headers, slug="agency-a", domain=HOST_A)
    await make_tenant(client, platform_headers, slug="agency-b", domain=HOST_B)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    secret = await _enrol_mfa(client, bearer(registered), app)

    challenge = await login_user(client, HOST_A, EMAIL, PASSWORD)
    token = challenge.json()["mfaToken"]

    cross = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfaToken": token, "code": pyotp.TOTP(secret).now()},
        headers={"Host": HOST_B},
    )
    assert cross.status_code == 401


async def test_mfa_secret_is_encrypted_at_rest(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    """§10.7: the raw column must never contain a usable TOTP seed."""
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    secret = await _enrol_mfa(client, bearer(registered), app)

    tenant_id = registered.json()["user"]["tenantId"]
    async with app.state.engine.begin() as conn:
        # `users` is under identity RLS: an unscoped connection sees only the
        # platform-staff partition, so the GUC has to be set for this read.
        await conn.execute(
            text("SELECT set_config('app.tenant_id', :tid, true)"), {"tid": tenant_id}
        )
        stored = (
            await conn.execute(
                text("SELECT mfa_secret FROM users WHERE email = :email"), {"email": EMAIL}
            )
        ).scalar_one()

    assert stored, "no secret stored"
    assert secret not in stored
    # The AES-GCM envelope carries the current key id (§10.7).
    assert stored.startswith(f"{get_settings().field_encryption_key_id}:")

    # And the ORM read path decrypts it transparently — the enrolled code works.
    verified = await client.post(
        "/api/v1/auth/mfa/verify",
        json={
            "mfaToken": (await login_user(client, HOST_A, EMAIL, PASSWORD)).json()["mfaToken"],
            "code": pyotp.TOTP(secret).now(),
        },
        headers={"Host": HOST_A},
    )
    assert verified.status_code == 200, verified.text


async def test_abandoned_re_enrolment_keeps_the_live_factor(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    """Re-enrolling (a lost phone) mints a *pending* secret; abandoning it
    before confirming must leave the original factor working — the enrolment
    must never overwrite the live secret before the new one is proven."""
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    access = bearer(registered)
    original_secret = await _enrol_mfa(client, access, app)

    # Start a re-enrolment (new secret handed out) but never confirm it.
    re_enrol = await client.post(
        "/api/v1/auth/mfa/enrol", headers={"Host": HOST_A, "Authorization": access}
    )
    assert re_enrol.status_code == 201
    new_secret = re_enrol.json()["secret"]
    assert new_secret != original_secret

    # A login-time code from the *original* authenticator still verifies: the
    # live factor was untouched by the abandoned re-enrolment.
    challenge = await login_user(client, HOST_A, EMAIL, PASSWORD)
    verified = await client.post(
        "/api/v1/auth/mfa/verify",
        json={"mfaToken": challenge.json()["mfaToken"], "code": pyotp.TOTP(original_secret).now()},
        headers={"Host": HOST_A},
    )
    assert verified.status_code == 200, verified.text


async def test_mfa_disable_requires_the_current_password(
    client: AsyncClient, platform_headers: dict[str, str], app: FastAPI
) -> None:
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    access = bearer(registered)
    await _enrol_mfa(client, access, app)

    wrong = await client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": "not-the-password"},
        headers={"Host": HOST_A, "Authorization": access},
    )
    assert wrong.status_code == 401

    ok = await client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": PASSWORD},
        headers={"Host": HOST_A, "Authorization": access},
    )
    assert ok.status_code == 204

    # With the factor gone, a password-only login is a full session again.
    plain = await login_user(client, HOST_A, EMAIL, PASSWORD)
    assert plain.status_code == 200
    assert plain.json()["accessToken"]


async def test_abandoned_enrolment_never_demands_a_factor(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """A secret exists but was never confirmed: login must stay unchanged, or
    a half-finished enrolment would lock someone out of their own account."""
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    enrol = await client.post(
        "/api/v1/auth/mfa/enrol",
        headers={"Host": HOST_A, "Authorization": bearer(registered)},
    )
    assert enrol.status_code == 201

    resp = await login_user(client, HOST_A, EMAIL, PASSWORD)
    assert resp.status_code == 200
    assert resp.json()["accessToken"]


async def test_mfa_confirm_rejects_a_wrong_code(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    registered = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    access = bearer(registered)
    await client.post("/api/v1/auth/mfa/enrol", headers={"Host": HOST_A, "Authorization": access})

    resp = await client.post(
        "/api/v1/auth/mfa/enrol/confirm",
        json={"code": "000000"},
        headers={"Host": HOST_A, "Authorization": access},
    )
    assert resp.status_code == 401
    status_resp = await client.get(
        "/api/v1/auth/mfa/status", headers={"Host": HOST_A, "Authorization": access}
    )
    assert status_resp.json()["enabled"] is False


# ---- OAuth seam (§7.1) ----


async def test_oauth_reports_not_configured_without_credentials(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)

    providers = await client.get("/api/v1/auth/oauth/providers", headers={"Host": HOST_A})
    assert providers.status_code == 200
    assert providers.json()["providers"] == []

    start = await client.post("/api/v1/auth/oauth/google/start", headers={"Host": HOST_A})
    assert start.status_code == 501, start.text
    assert start.json()["type"].endswith("feature-not-configured")

    callback = await client.post(
        "/api/v1/auth/oauth/google/callback",
        json={"code": "x", "state": "y"},
        headers={"Host": HOST_A},
    )
    assert callback.status_code == 501


async def test_oauth_unknown_provider_is_also_not_configured(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    resp = await client.post("/api/v1/auth/oauth/nosuch/start", headers={"Host": HOST_A})
    assert resp.status_code == 501


def test_oauth_registry_builds_google_when_credentials_are_present() -> None:
    """The seam flips on with config alone — no code change (§7.1)."""
    from app.integrations.auth_oauth.registry import (
        build_oauth_provider,
        configured_oauth_providers,
    )

    settings = get_settings().model_copy(
        update={
            "oauth_google_client_id": "client-id",
            "oauth_google_client_secret": "client-secret",
            "oauth_redirect_base_url": "https://agency-a.test",
        }
    )
    provider = build_oauth_provider(settings, "google")
    assert provider is not None
    assert provider.key == "google"
    assert configured_oauth_providers(settings) == ["google"]

    url = provider.authorization_url(
        redirect_uri="https://agency-a.test/api/v1/auth/oauth/google/callback",
        state="state-value",
    )
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert "state=state-value" in url
    assert "client_id=client-id" in url


# ---- session list + revoke (§10.3) ----


async def test_session_list_and_revoke_other_device(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    first = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    second = await login_user(client, HOST_A, EMAIL, PASSWORD)

    from tests.helpers import use_refresh_cookie

    use_refresh_cookie(client, refresh_cookie(second))
    listed = await client.get(
        "/api/v1/auth/sessions", headers={"Host": HOST_A, "Authorization": bearer(second)}
    )
    assert listed.status_code == 200, listed.text
    rows = listed.json()
    assert len(rows) == 2
    # Exactly one row is flagged as the caller's own — the presented refresh
    # cookie is what identifies "this device".
    assert sum(1 for r in rows if r["current"]) == 1
    assert all("token" not in key.lower() for row in rows for key in row)

    other = next(r for r in rows if not r["current"])
    revoked = await client.delete(
        f"/api/v1/auth/sessions/{other['id']}",
        headers={"Host": HOST_A, "Authorization": bearer(second)},
    )
    assert revoked.status_code == 204

    # The revoked device's refresh chain is dead...
    use_refresh_cookie(client, refresh_cookie(first))
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})).status_code == 401

    # ...and the caller's own still works.
    use_refresh_cookie(client, refresh_cookie(second))
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})).status_code == 200


async def test_cannot_revoke_another_users_session(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """404, not 403 — the endpoint must not confirm that a session id exists."""
    await make_tenant(client, platform_headers)
    victim = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    attacker = await register_user(client, HOST_A, email="other@example.com", password=PASSWORD)

    from tests.helpers import use_refresh_cookie

    use_refresh_cookie(client, refresh_cookie(victim))
    victim_sessions = await client.get(
        "/api/v1/auth/sessions", headers={"Host": HOST_A, "Authorization": bearer(victim)}
    )
    target = victim_sessions.json()[0]["id"]

    resp = await client.delete(
        f"/api/v1/auth/sessions/{target}",
        headers={"Host": HOST_A, "Authorization": bearer(attacker)},
    )
    assert resp.status_code == 404

    # The victim's session is untouched.
    use_refresh_cookie(client, refresh_cookie(victim))
    assert (await client.post("/api/v1/auth/refresh", headers={"Host": HOST_A})).status_code == 200


async def test_revoked_sessions_drop_out_of_the_list(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    await make_tenant(client, platform_headers)
    first = await register_user(client, HOST_A, email=EMAIL, password=PASSWORD)
    second = await login_user(client, HOST_A, EMAIL, PASSWORD)

    from tests.helpers import use_refresh_cookie

    use_refresh_cookie(client, refresh_cookie(first))
    await client.post(
        "/api/v1/auth/logout", headers={"Host": HOST_A, "Authorization": bearer(first)}
    )

    use_refresh_cookie(client, refresh_cookie(second))
    listed = await client.get(
        "/api/v1/auth/sessions", headers={"Host": HOST_A, "Authorization": bearer(second)}
    )
    assert len(listed.json()) == 1
    assert listed.json()[0]["current"] is True


# ------------------------------------------ Argon2 cost budget (PYT-01) ---


def test_argon2_constants_match_the_hasher_actually_in_use() -> None:
    """The documented cost parameters drive the deployment memory budget
    (``scripts/check_argon2_budget.py``), so they must not drift from what
    pwdlib resolves — a stale constant would under-state the real footprint and
    the container would OOM under an authentication burst.
    """
    hasher = security._password_hasher.hashers[0]._hasher
    assert hasher.memory_cost == security.ARGON2_MEMORY_MIB * 1024
    assert hasher.time_cost == security.ARGON2_TIME_COST
    assert hasher.parallelism == security.ARGON2_PARALLELISM


def test_argon2_memory_cost_stays_at_the_owasp_floor() -> None:
    """Lowering the memory cost is what makes cracking a stolen hash cheaper;
    if it is ever reduced, that must be a deliberate, reviewed change."""
    assert security.ARGON2_MEMORY_MIB >= 64


@pytest.mark.parametrize(
    ("limit_mib", "workers", "expected_exit"),
    [
        (512, 2, 0),  # the §16 default topology fits the audit's 512 MiB pod
        (512, 4, 1),  # doubling workers without raising the limit does not
        (1024, 4, 0),  # ...and raising the limit fixes it
    ],
)
def test_argon2_budget_check_gates_on_the_memory_limit(
    monkeypatch: pytest.MonkeyPatch, limit_mib: int, workers: int, expected_exit: int
) -> None:
    """The checker must actually fail a deploy that would OOM, not just print."""
    from scripts import check_argon2_budget

    monkeypatch.setattr(
        "sys.argv",
        [
            "check_argon2_budget.py",
            "--limit-mib",
            str(limit_mib),
            "--web-concurrency",
            str(workers),
        ],
    )
    assert check_argon2_budget.main() == expected_exit
