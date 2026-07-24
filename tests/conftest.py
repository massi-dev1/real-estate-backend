"""Shared fixtures. Tests run against the real docker-compose services
(Postgres+PostGIS on realestate_test, Redis db 1, Mailpit SMTP) — never
SQLite (§13)."""

import os

# Must be set before anything imports app.core.config.
os.environ["APP_ENV"] = "local"
os.environ["APP_SECRET_KEY"] = "test-secret-key-0123456789abcdef0123456789abcdef"
os.environ["FIELD_ENCRYPTION_KEY"] = "test-field-key-fedcba9876543210fedcba9876543210"
os.environ["DATABASE_URL"] = (
    "postgresql+asyncpg://app_user:app_password@localhost:5432/realestate_test"
)
os.environ["DATABASE_DDL_URL"] = (
    "postgresql+asyncpg://postgres:postgres@localhost:5432/realestate_test"
)
os.environ["REDIS_URL"] = "redis://localhost:6379/1"
os.environ["CELERY_BROKER_URL"] = "redis://localhost:6379/1"
os.environ["CELERY_RESULT_BACKEND"] = "redis://localhost:6379/1"
# Dedicated MinIO buckets (created by the compose minio-init one-shot), so
# test objects never mix with dev data.
os.environ["STORAGE_ACCESS_KEY"] = "minio"
os.environ["STORAGE_SECRET_KEY"] = "minio12345"
os.environ["STORAGE_MEDIA_BUCKET"] = "media-test"
os.environ["STORAGE_DOCS_BUCKET"] = "media-private-test"

import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.config import get_settings
from app.core.database import set_tenant_guc
from app.core.permissions import Role
from app.core.security import hash_password
from app.main import create_app
from app.modules.users.models import User
from app.workers.celery_app import celery_app

# Tasks run inline, synchronously, in-process — no live broker/worker needed
# for `.delay()` calls exercised by the test suite (§13 keeps this a real
# Postgres/Redis suite; Celery eager mode is the one deliberate fake here,
# since a real worker process is out of scope for API-level tests).
celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

PLATFORM_ADMIN_EMAIL = "root@platform.example.com"
# One password (and one Argon2 hash, computed once) shared by all fixture-made
# accounts — hashing per test would add ~50ms each.
FIXTURE_PASSWORD = "Fixture-Pass-123456"
_FIXTURE_PASSWORD_HASH = hash_password(FIXTURE_PASSWORD)


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
    """Each test starts from empty tables and a cold Redis cache."""
    yield
    async with app.state.engine.begin() as conn:
        # Tenant users/sessions cascade from tenants; the second DELETE removes
        # platform staff (the identity RLS policy exposes exactly the
        # NULL-tenant rows to this unscoped connection).
        await conn.execute(text("DELETE FROM tenants"))
        await conn.execute(text("DELETE FROM users"))
    await app.state.redis.flushdb()


@pytest.fixture
async def platform_headers(app: FastAPI, client: AsyncClient) -> dict[str, str]:
    """A logged-in platform admin's Authorization header (replaces the Part 2
    API-key stopgap)."""
    async with app.state.session_factory() as session, session.begin():
        session.add(
            User(
                tenant_id=None,
                email=PLATFORM_ADMIN_EMAIL,
                password_hash=_FIXTURE_PASSWORD_HASH,
                role=Role.PLATFORM_ADMIN,
            )
        )
    resp = await client.post(
        "/api/v1/platform/auth/login",
        json={"email": PLATFORM_ADMIN_EMAIL, "password": FIXTURE_PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['accessToken']}"}


@pytest.fixture
def create_tenant_user(
    app: FastAPI,
) -> Callable[..., Awaitable[uuid.UUID]]:
    """Insert a tenant user directly (with the tenant GUC set, as RLS demands).

    Used to provision the first admin of a tenant — the API path for that
    (tenant onboarding) is a later part.
    """

    async def _create(tenant_id: str | uuid.UUID, email: str, role: Role) -> uuid.UUID:
        tid = uuid.UUID(str(tenant_id))
        user = User(tenant_id=tid, email=email, password_hash=_FIXTURE_PASSWORD_HASH, role=role)
        async with app.state.session_factory() as session, session.begin():
            await set_tenant_guc(session, tid)
            session.add(user)
        return user.id

    return _create


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
