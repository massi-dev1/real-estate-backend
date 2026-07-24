"""Observability (§14): Prometheus metrics, and the offline-safety guarantee
that Sentry/OTEL stay inert without credentials."""

from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from prometheus_client.parser import text_string_to_metric_families

from app.core import metrics as metrics_module
from app.core import telemetry
from app.core.config import Settings, get_settings
from app.core.metrics import LEADS_CREATED, NOTIFICATION_SENDS, MetricsMiddleware
from app.core.telemetry import (
    _scrub_event,
    init_sentry,
    init_tracing,
    instrument_app,
    tracing_configured,
)
from app.main import create_app
from tests.helpers import HOST_A
from tests.test_leads import capture, capture_body
from tests.test_tenants_platform_api import create_tenant

METRICS_URL = "/internal/metrics"


def sample_value(text: str, metric: str, **labels: str) -> float:
    """The value of one labelled sample in a Prometheus exposition payload."""
    for family in text_string_to_metric_families(text):
        for s in family.samples:
            if s.name == metric and all(s.labels.get(k) == v for k, v in labels.items()):
                return float(s.value)
    return 0.0


def counter_total(counter: Any, **labels: str) -> float:
    """Read a counter's current value straight off the client object — lets a
    test measure a delta without re-scraping."""
    return float(counter.labels(**labels)._value.get())


async def test_metrics_endpoint_returns_prometheus_text(client: AsyncClient) -> None:
    resp = await client.get(METRICS_URL)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    body = resp.text
    # The HTTP family plus the §14 infrastructure gauges are all present.
    assert "http_requests_total" in body
    assert "http_request_duration_seconds" in body
    assert "db_pool_connections" in body
    assert "celery_queue_depth" in body
    assert "cache_hit_ratio" in body


async def test_request_metrics_increment_by_route_template(client: AsyncClient) -> None:
    before = sample_value(
        (await client.get(METRICS_URL)).text,
        "http_requests_total",
        method="GET",
        route="/healthz",
        status="200",
    )
    await client.get("/healthz")
    after = sample_value(
        (await client.get(METRICS_URL)).text,
        "http_requests_total",
        method="GET",
        route="/healthz",
        status="200",
    )
    assert after == before + 1


async def test_metrics_label_route_template_not_raw_path(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """Labelling by raw path would mint one time series per listing id and blow
    up the scraper's cardinality — the label must be the full route template,
    so two different refs collapse into one series."""
    await create_tenant(client, platform_headers)
    route = "/api/v1/listings/{ref_or_id}"

    before = sample_value(
        (await client.get(METRICS_URL)).text,
        "http_requests_total",
        method="GET",
        route=route,
        status="404",
    )
    for ref in ("does-not-exist-ref", "another-missing-ref"):
        assert (await client.get(f"/api/v1/listings/{ref}", headers={"Host": HOST_A})).status_code
    body = (await client.get(METRICS_URL)).text

    assert "does-not-exist-ref" not in body
    # Full template, not the mount-relative "/listings/{ref_or_id}".
    assert f'route="{route}"' in body
    assert (
        sample_value(body, "http_requests_total", method="GET", route=route, status="404")
        == before + 2
    )


async def test_queue_depth_is_sampled_from_the_broker_client(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gauge must read the *broker* client, not the cache one.

    In production these are different Redis databases (`redis_url` db 0 vs
    `celery_broker_url` db 2), so sampling the cache client LLENs keys that only
    ever exist on the broker and reports 0 for every queue — blind during
    exactly the backlog §14 alerts on. `conftest` pins both URLs to db 1, which
    makes the two clients interchangeable and any value-based assertion
    vacuous; so this asserts on *which client is passed through*, which is the
    thing that was actually wrong.
    """
    seen: dict[str, Any] = {}
    real = metrics_module.collect_runtime_metrics

    async def _spy(engine: Any, redis: Any, broker: Any = None) -> None:
        seen["redis"] = redis
        seen["broker"] = broker
        await real(engine, redis, broker)

    monkeypatch.setattr("app.internal.collect_runtime_metrics", _spy)
    assert (await client.get(METRICS_URL)).status_code == 200

    assert seen["broker"] is app.state.broker_redis
    assert seen["broker"] is not seen["redis"]

    # And the depth it reports is real: push onto the broker, scrape, read back.
    monkeypatch.undo()
    broker: Any = app.state.broker_redis
    await broker.rpush("media", "task-a", "task-b", "task-c")
    try:
        body = (await client.get(METRICS_URL)).text
        assert sample_value(body, "celery_queue_depth", queue="media") == 3.0
    finally:
        await broker.delete("media")


async def test_queue_depth_failure_does_not_blind_remaining_queues(
    app: FastAPI, client: AsyncClient
) -> None:
    """One unreadable queue must `continue`, not `break` — a `break` left every
    later queue in CELERY_QUEUES silently holding a stale value."""
    broker: Any = app.state.broker_redis
    # "default" sorts first in CELERY_QUEUES; a string key makes LLEN raise
    # WRONGTYPE, which is exactly the per-queue failure the loop must absorb.
    await broker.set("default", "not-a-list")
    await broker.rpush("analytics", "task-a")
    try:
        body = (await client.get(METRICS_URL)).text
        # The queue *after* the failing one was still sampled.
        assert sample_value(body, "celery_queue_depth", queue="analytics") == 1.0
    finally:
        await broker.delete("default", "analytics")


async def test_metrics_endpoint_requires_token_when_configured(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configured scrape token is enforced; a wrong/absent one 404s rather
    than 403s so a caller learns nothing about the deployment."""
    settings: Settings = app.state.settings
    monkeypatch.setattr(settings, "metrics_auth_token", "scrape-secret")

    assert (await client.get(METRICS_URL)).status_code == 404
    bad = await client.get(METRICS_URL, headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 404
    good = await client.get(METRICS_URL, headers={"Authorization": "Bearer scrape-secret"})
    assert good.status_code == 200


async def test_metrics_endpoint_hidden_when_disabled(
    app: FastAPI, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(app.state.settings, "metrics_enabled", False)
    assert (await client.get(METRICS_URL)).status_code == 404


async def test_lead_capture_increments_business_counter(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """§14 leads/hour: `rate(leads_created_total[1h])`."""
    await create_tenant(client, platform_headers)
    before = counter_total(LEADS_CREATED, source="other")

    resp = await capture(client, capture_body(email="metrics-buyer@example.com"))
    assert resp.status_code == 201, resp.text

    assert counter_total(LEADS_CREATED, source="other") == before + 1


async def test_honeypot_capture_does_not_count_a_lead(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """A bot hit persists nothing, so it must not inflate leads/hour either —
    the counter is registered post-commit, and a honeypot capture never commits
    a lead row."""
    await create_tenant(client, platform_headers)
    before = counter_total(LEADS_CREATED, source="other")

    resp = await capture(client, capture_body(email="bot@example.com", hp="gotcha"))
    assert resp.status_code == 201, resp.text

    assert counter_total(LEADS_CREATED, source="other") == before


async def test_notification_delivery_increments_counter(
    client: AsyncClient, platform_headers: dict[str, str]
) -> None:
    """§14 delivery rate: sent / (sent + failed), counted at the single write
    point for the delivery log so metric and audit table agree."""
    await create_tenant(client, platform_headers)
    before = counter_total(NOTIFICATION_SENDS, channel="email", status="sent")

    # A capture assigns the lead and notifies the agent over email (Part 18),
    # which runs the eager deliver_notification task.
    resp = await capture(client, capture_body(email="notify-metrics@example.com"))
    assert resp.status_code == 201, resp.text

    after = counter_total(NOTIFICATION_SENDS, channel="email", status="sent")
    assert after >= before


# ---- offline safety: telemetry stays inert without credentials ----


def test_sentry_and_tracing_skipped_when_unconfigured() -> None:
    settings = get_settings()
    assert settings.sentry_dsn == ""
    assert settings.otel_enabled is False
    assert init_sentry(settings, component="api") is False
    assert init_tracing(settings) is False


def test_tracing_skipped_without_endpoint_even_when_enabled() -> None:
    """`otel_enabled` alone is not enough — an exporter with no endpoint would
    fail at export time, so the flag pair must both be set."""
    settings = get_settings().model_copy(
        update={"otel_enabled": True, "otel_exporter_endpoint": ""}
    )
    assert init_tracing(settings) is False


def test_app_boots_with_all_telemetry_flags_off() -> None:
    """The whole point of §14's config gating: no exporter credentials
    anywhere, and the app still constructs — with metrics (credential-free)
    wired in and the exporters skipped."""
    app = create_app()
    assert app.state.settings.sentry_dsn == ""
    assert app.state.settings.otel_enabled is False
    assert MetricsMiddleware in [m.cls for m in app.user_middleware]


def test_instrumentation_skipped_on_flag_without_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """`instrument_app` must apply the same both-halves check `init_tracing`
    does. Guarding on `otel_enabled` alone instrumented every request and DB
    statement against a provider that was never installed — paying span cost on
    the hottest paths for spans that are immediately discarded."""
    settings = get_settings().model_copy(
        update={"otel_enabled": True, "otel_exporter_endpoint": ""}
    )
    assert tracing_configured(settings) is False

    # If the guard is wrong, instrument_app reaches FastAPIInstrumentor and
    # flips the module's applied-once flag; a correct guard returns first.
    monkeypatch.setattr(telemetry, "_otel_instrumented", False)
    instrument_app(create_app(), None, settings)
    assert telemetry._otel_instrumented is False


@pytest.mark.parametrize("cookie_header", ["Cookie", "cookie"])
def test_sentry_scrubber_redacts_raw_cookie_header(cookie_header: str) -> None:
    """The parsed `cookies` dict and the raw `Cookie` header are different
    fields on a Sentry event. Dropping only the former shipped a live
    `refresh_token` session cookie on any authenticated 500 (§10.12) — the
    log-field denylist has no `cookie` key to match it against."""
    event: Any = {
        "request": {"headers": {cookie_header: "refresh_token=live-session-value"}},
    }
    scrubbed: Any = _scrub_event(event, {})

    assert scrubbed["request"]["headers"][cookie_header] == "[redacted]"
    assert "live-session-value" not in str(scrubbed)


@pytest.mark.parametrize("secret_header", ["Authorization", "authorization"])
def test_sentry_scrubber_redacts_credentials(secret_header: str) -> None:
    """Sentry captures request data the structlog redactor never sees, so the
    same key denylist is applied on the way out (§10.12)."""
    event: Any = {
        "request": {
            "headers": {secret_header: "Bearer super-secret", "User-Agent": "curl"},
            "cookies": {"refresh_token": "opaque"},
            "data": {"password": "hunter2"},
        },
        "extra": {"access_token": "leaked", "listing_id": "keep-me"},
    }
    scrubbed: Any = _scrub_event(event, {})

    assert scrubbed["request"]["headers"][secret_header] == "[redacted]"
    assert scrubbed["request"]["headers"]["User-Agent"] == "curl"
    assert "cookies" not in scrubbed["request"]
    assert "data" not in scrubbed["request"]
    assert scrubbed["extra"]["access_token"] == "[redacted]"
    assert scrubbed["extra"]["listing_id"] == "keep-me"
