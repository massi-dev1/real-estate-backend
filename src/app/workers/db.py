"""DB access for Celery tasks.

Tasks run in a separate process from the FastAPI app and cannot share its
pooled engine/event loop, so each call site opens a short-lived engine of its
own (``run_scoped``) — or one shared engine for a whole batch of scoped calls
(``run_scoped_many``, for jobs that loop over many tenants). Each individual
transaction still mirrors ``core.database.get_session``: ``SET LOCAL
app.tenant_id`` when a tenant is given, commit on success.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import get_settings
from app.core.database import create_engine, create_session_factory, set_tenant_guc


def _run_with_current_app[T](coro: Awaitable[T]) -> T:
    """Celery's "current app" lookup is thread-local: ``set_as_current`` only
    pushed our app onto the main thread's stack, so a fresh worker thread
    would otherwise fall back to an unconfigured default app — silently
    dropping ``task_always_eager`` for any nested ``.delay()`` call (e.g. the
    lead-drip sweep task emailing via ``send_email.delay()``). Imported
    lazily (not at module scope) to dodge the circular import: celery_app
    pulls in the task modules, which pull in this one.
    """
    from app.workers.celery_app import celery_app

    celery_app.set_current()
    return asyncio.run(coro)  # type: ignore[arg-type]


def run_sync[T](coro: Awaitable[T]) -> T:
    """Run an awaitable from a sync Celery task body.

    A real worker process (prefork, no asyncio) always takes the plain
    ``asyncio.run`` path. Celery's eager mode (used by the test suite, §13)
    executes the task body inline inside pytest-asyncio's already-running
    loop, where ``asyncio.run`` would raise — that case gets its own loop on
    a worker thread instead.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # type: ignore[arg-type]
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_run_with_current_app, coro).result()


async def _scoped_transaction[T](
    session_factory: async_sessionmaker[AsyncSession],
    tenant_id: uuid.UUID | None,
    fn: Callable[[AsyncSession], Awaitable[T]],
) -> T:
    async with session_factory() as session:
        async with session.begin():
            if tenant_id is not None:
                await set_tenant_guc(session, tenant_id)
            result = await fn(session)
        # Drain post-commit callbacks after a successful commit — mirrors
        # ``core.database.get_session`` so a task body that calls a service
        # registering ``on_commit`` side effects (e.g. ``notify()``'s WS push
        # and external-send enqueue) fires them exactly as a request would.
        callbacks: list[Callable[[], Awaitable[None]]]
        callbacks = session.info.get("post_commit_callbacks", [])
        for callback in callbacks:
            await callback()
        return result


async def _run_scoped[T](
    tenant_id: uuid.UUID | None, fn: Callable[[AsyncSession], Awaitable[T]]
) -> T:
    engine = create_engine(get_settings())
    try:
        session_factory = create_session_factory(engine)
        return await _scoped_transaction(session_factory, tenant_id, fn)
    finally:
        await engine.dispose()


def run_scoped[T](tenant_id: uuid.UUID | None, fn: Callable[[AsyncSession], Awaitable[T]]) -> T:
    """Sync entry point for Celery task bodies: run ``fn`` in one tenant-scoped
    transaction, safely under both a real worker and eager-mode tests."""
    return run_sync(_run_scoped(tenant_id, fn))


async def _run_scoped_many[T](
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[T]]]],
) -> list[T]:
    engine = create_engine(get_settings())
    try:
        session_factory = create_session_factory(engine)
        return [
            await _scoped_transaction(session_factory, tenant_id, fn)
            for tenant_id, fn in calls
        ]
    finally:
        await engine.dispose()


def run_scoped_many[T](
    calls: list[tuple[uuid.UUID | None, Callable[[AsyncSession], Awaitable[T]]]],
) -> list[T]:
    """Like ``run_scoped``, but runs a sequence of scoped transactions on one
    shared engine — for batch jobs that loop over many tenants, where opening
    a fresh engine per tenant is pure administrative overhead (SET LOCAL
    already isolates each transaction on a shared pool)."""
    return run_sync(_run_scoped_many(calls))
