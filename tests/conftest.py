"""Shared fixtures. Tests run against the real docker-compose services
(Postgres+PostGIS on realestate_test, Redis db 1) — never SQLite (§13)."""

import os

# Must be set before anything imports app.core.config.
os.environ["APP_ENV"] = "local"
os.environ["APP_SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://app_user:app_password@localhost:5432/realestate_test"
)
os.environ["DATABASE_DDL_URL"] = (
    "postgresql+asyncpg://postgres:postgres@localhost:5432/realestate_test"
)
os.environ["REDIS_URL"] = "redis://localhost:6379/1"
os.environ["PLATFORM_API_KEY"] = "test-platform-key-0123456789abcdef"

import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.main import create_app

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PLATFORM_HEADERS = {"X-Platform-Key": os.environ["PLATFORM_API_KEY"]}


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Rebuild the test schema from migrations (also exercises downgrade)."""
    for direction in (["downgrade", "base"], ["upgrade", "head"]):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *direction],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"alembic {' '.join(direction)} failed:\n{result.stderr}")


@pytest.fixture(scope="session")
async def app() -> AsyncIterator[FastAPI]:
    get_settings.cache_clear()
    application = create_app()
    async with LifespanManager(application):
        yield application


@pytest.fixture(autouse=True)
async def _clean_state(app: FastAPI) -> AsyncIterator[None]:
    """Each test starts from empty tenant tables and a cold Redis cache."""
    yield
    async with app.state.engine.begin() as conn:
        await conn.execute(text("DELETE FROM tenants"))
    await app.state.redis.flushdb()


@pytest.fixture
def platform_headers() -> dict[str, str]:
    return dict(PLATFORM_HEADERS)


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
