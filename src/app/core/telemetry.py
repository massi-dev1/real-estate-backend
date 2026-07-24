"""Error tracking (Sentry) and distributed tracing (OpenTelemetry) — §14.

Both are **opt-in and offline-safe**: an empty ``sentry_dsn`` or
``otel_enabled=False`` makes every function here a no-op, so the app boots and
the test suite runs with no exporter credentials anywhere (same stance as the
AI and billing stubs). Nothing in this module raises — a telemetry backend
being misconfigured must never stop the app from serving traffic.

The init functions are idempotent by design: the Celery worker and the API run
in separate processes, but eager-mode tests and repeated ``create_app()`` calls
share one, and OTEL's instrumentors raise if applied twice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

from app.core.config import Settings
from app.core.logging import REDACTED_KEYS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI
    from sentry_sdk.types import Event, Hint
    from sqlalchemy.ext.asyncio import AsyncEngine

logger = structlog.get_logger(__name__)

_otel_instrumented = False


# ---- Sentry ----


def _scrub_event(event: Event, _hint: Hint) -> Event:
    """Second line of PII defence before an event leaves the process (§10.12).

    ``core/logging.py``'s redaction covers what we *log*; Sentry also captures
    request data and local variables the logger never sees, so the same key
    denylist is applied to the outbound event. Sentry's own ``send_default_pii``
    stays off, which is what keeps bodies/cookies out in the first place.
    """
    request: Any = event.get("request")
    if isinstance(request, dict):
        headers = request.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower().replace("-", "_") in REDACTED_KEYS:
                    headers[key] = "[redacted]"
        request.pop("cookies", None)
        request.pop("data", None)

    extra: Any = event.get("extra")
    if isinstance(extra, dict):
        for key in extra.keys() & REDACTED_KEYS:
            extra[key] = "[redacted]"
    return event


def init_sentry(settings: Settings, *, component: str) -> bool:
    """Initialise Sentry when a DSN is configured. Returns whether it ran.

    ``component`` distinguishes the API process from the Celery worker in the
    Sentry UI; the integrations are auto-enabled by the SDK for whichever of
    FastAPI/Celery/SQLAlchemy is importable in that process.
    """
    if not settings.sentry_dsn:
        return False

    import sentry_sdk

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.app_env,
        release=settings.sentry_release or None,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        # Never let the SDK attach request bodies, cookies or user identifiers
        # on its own — everything PII-bearing must pass _scrub_event first.
        send_default_pii=False,
        before_send=_scrub_event,
    )
    sentry_sdk.set_tag("component", component)
    logger.info("sentry_initialised", component=component, env=settings.app_env)
    return True


# ---- OpenTelemetry ----


def init_tracing(settings: Settings) -> bool:
    """Configure the OTLP tracer provider. Returns whether tracing is on.

    Instrumenting the frameworks is a separate step
    (:func:`instrument_app`/:func:`instrument_celery`) because the FastAPI app
    and the SQLAlchemy engine only exist later in startup.
    """
    if not settings.otel_enabled or not settings.otel_exporter_endpoint:
        return False

    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(
        resource=Resource.create(
            {"service.name": settings.otel_service_name, "deployment.environment": settings.app_env}
        )
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=settings.otel_exporter_endpoint))
    )
    trace.set_tracer_provider(provider)
    logger.info("otel_initialised", endpoint=settings.otel_exporter_endpoint)
    return True


def _request_hook(span: Any, scope: dict[str, Any]) -> None:
    """Stamp the ``X-Request-ID`` correlation value onto the server span, so a
    trace can be found from a log line (``RequestContextMiddleware`` binds the
    same id into every structlog event)."""
    if span is None or not span.is_recording():
        return
    headers = dict(scope.get("headers") or [])
    request_id = headers.get(b"x-request-id", b"").decode("latin-1")
    if request_id:
        span.set_attribute("request.id", request_id)


def instrument_app(app: FastAPI, engine: AsyncEngine | None, settings: Settings) -> None:
    """Instrument FastAPI + SQLAlchemy once tracing is configured and the
    engine exists (called from the lifespan, not the app factory)."""
    global _otel_instrumented
    if not settings.otel_enabled or _otel_instrumented:
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app, server_request_hook=_request_hook)

        if engine is not None:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            # The async engine wraps a sync one; that inner engine is what
            # SQLAlchemy's event hooks fire on.
            SQLAlchemyInstrumentor().instrument(engine=engine.sync_engine)
        _otel_instrumented = True
        logger.info("otel_instrumented", target="fastapi+sqlalchemy")
    except Exception:
        # Telemetry wiring must never break startup.
        logger.warning("otel_instrumentation_failed", exc_info=True)


def instrument_celery(settings: Settings) -> None:
    """Instrument Celery in the worker process (called from ``worker_process_init``)."""
    if not settings.otel_enabled:
        return
    try:
        from opentelemetry.instrumentation.celery import CeleryInstrumentor

        # The instrumentation packages ship no py.typed marker, so their
        # constructors read as untyped under mypy strict.
        CeleryInstrumentor().instrument()  # type: ignore[no-untyped-call]
        logger.info("otel_instrumented", target="celery")
    except Exception:
        logger.warning("otel_instrumentation_failed", exc_info=True)
