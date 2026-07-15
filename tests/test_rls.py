"""Row-level security mechanism (§4.2), proven against a scratch table.

Creates an RLS-enabled table as the owner role, then verifies through the
app engine (non-superuser ``app_user``) that: unscoped sessions fail closed,
scoped sessions see only their tenant, and cross-tenant writes are rejected.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.database import set_tenant_guc
from app.core.rls import enable_tenant_rls_sql

TENANT_A = uuid.uuid4()
TENANT_B = uuid.uuid4()


@pytest.fixture
async def rls_probe() -> AsyncIterator[None]:
    ddl_engine = create_async_engine(os.environ["DATABASE_DDL_URL"])
    async with ddl_engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE rls_probe (tenant_id uuid NOT NULL, val text NOT NULL)")
        )
        for stmt in enable_tenant_rls_sql("rls_probe"):
            await conn.execute(text(stmt))
        await conn.execute(
            text("INSERT INTO rls_probe VALUES (CAST(:a AS uuid), 'a'), (CAST(:b AS uuid), 'b')"),
            {"a": str(TENANT_A), "b": str(TENANT_B)},
        )
    try:
        yield
    finally:
        async with ddl_engine.begin() as conn:
            await conn.execute(text("DROP TABLE rls_probe"))
        await ddl_engine.dispose()


async def test_unscoped_session_fails_closed(app: FastAPI, rls_probe: None) -> None:
    async with app.state.session_factory() as session, session.begin():
        with pytest.raises(DBAPIError):
            await session.execute(text("SELECT val FROM rls_probe"))


async def test_scoped_session_sees_only_its_tenant(app: FastAPI, rls_probe: None) -> None:
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, TENANT_A)
        rows = (await session.execute(text("SELECT val FROM rls_probe"))).scalars().all()
        assert rows == ["a"]


async def test_scoped_session_can_write_own_tenant(app: FastAPI, rls_probe: None) -> None:
    async with app.state.session_factory() as session, session.begin():
        await set_tenant_guc(session, TENANT_A)
        await session.execute(
            text("INSERT INTO rls_probe VALUES (CAST(:a AS uuid), 'a2')"),
            {"a": str(TENANT_A)},
        )
        count = (await session.execute(text("SELECT count(*) FROM rls_probe"))).scalar_one()
        assert count == 2


async def test_cross_tenant_write_is_rejected(app: FastAPI, rls_probe: None) -> None:
    async with app.state.session_factory() as session:
        await session.begin()
        await set_tenant_guc(session, TENANT_A)
        with pytest.raises(DBAPIError):
            await session.execute(
                text("INSERT INTO rls_probe VALUES (CAST(:b AS uuid), 'sneaky')"),
                {"b": str(TENANT_B)},
            )
        await session.rollback()
