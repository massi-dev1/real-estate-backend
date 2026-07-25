"""Shared helpers for auth-flow tests: tenant-scoped requests, refresh-cookie
plumbing, and reading one-time codes back out of Mailpit."""

import os
import re
from typing import Any

import httpx
from httpx import AsyncClient, Response

# Read from the environment so the testcontainers path (tests/containers.py)
# can point the suite at a random-port Mailpit; falls back to the fixed
# compose/CI port when reusing a running stack.
MAILPIT_URL = os.environ.get("MAILPIT_URL", "http://localhost:8025")
HOST_A = "agency-a.test"
HOST_B = "agency-b.test"


async def register_user(
    client: AsyncClient,
    host: str,
    *,
    email: str = "buyer@example.com",
    password: str = "Buyer-Pass-123456",
    **fields: Any,
) -> Response:
    return await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, **fields},
        headers={"Host": host},
    )


async def login_user(client: AsyncClient, host: str, email: str, password: str) -> Response:
    return await client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": password},
        headers={"Host": host},
    )


def bearer(resp_or_token: Response | str) -> str:
    token = resp_or_token if isinstance(resp_or_token, str) else resp_or_token.json()["accessToken"]
    return f"Bearer {token}"


def refresh_cookie(resp: Response) -> str:
    value = resp.cookies.get("refresh_token")
    assert value, "response did not set a refresh cookie"
    return value


def use_refresh_cookie(client: AsyncClient, value: str) -> None:
    """Make the next requests present exactly this refresh token."""
    # No domain= here: cookiejar treats the dotless "testserver" host as
    # "testserver.local", so an explicit domain never matches.
    client.cookies.clear()
    client.cookies.set("refresh_token", value, path="/api/v1/auth")


async def mailpit_code(to: str, subject_word: str) -> str:
    """Fetch the newest one-time code Mailpit received for ``to``."""
    async with httpx.AsyncClient() as mailpit:
        resp = await mailpit.get(
            f"{MAILPIT_URL}/api/v1/search",
            params={"query": f"to:{to} subject:{subject_word}"},
        )
        resp.raise_for_status()
        messages = resp.json()["messages"]
        assert messages, f"no '{subject_word}' email delivered to {to}"
        resp = await mailpit.get(f"{MAILPIT_URL}/api/v1/message/{messages[0]['ID']}")
        resp.raise_for_status()
        match = re.search(r"code: (\S+)", resp.json()["Text"])
        assert match, "email did not contain a code"
        return match.group(1)
