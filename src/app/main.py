"""App factory: middleware, exception handlers, router mounting, lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.openapi.utils import get_openapi
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.cors import TenantCORSMiddleware
from app.core.database import create_engine, create_session_factory
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.metrics import MetricsMiddleware
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.rate_limit import GlobalRateLimitMiddleware
from app.core.storage import create_storage
from app.core.telemetry import init_sentry, init_tracing, instrument_app
from app.core.tenancy import TenantResolutionMiddleware
from app.health import router as health_router
from app.internal import router as internal_router
from app.modules.agents.router import portal_router as agents_portal_router
from app.modules.agents.router import public_router as agents_public_router
from app.modules.agents.router import teams_router
from app.modules.analytics.router import listing_report_router as analytics_listing_report_router
from app.modules.analytics.router import portal_router as analytics_portal_router
from app.modules.analytics.router import public_router as analytics_public_router
from app.modules.appointments.router import (
    booking_idempotent_router as appointments_booking_idempotent_router,
)
from app.modules.appointments.router import me_router as appointments_me_router
from app.modules.appointments.router import portal_router as appointments_portal_router
from app.modules.appointments.router import public_router as appointments_public_router
from app.modules.auth.router import auth_router, platform_auth_router
from app.modules.blog.router import portal_router as blog_portal_router
from app.modules.blog.router import public_router as blog_public_router
from app.modules.compliance.router import me_router as compliance_me_router
from app.modules.compliance.router import portal_router as compliance_portal_router
from app.modules.compliance.router import public_router as compliance_public_router
from app.modules.compliance.router import site_router as compliance_site_router
from app.modules.content.router import portal_router as content_portal_router
from app.modules.content.router import public_router as content_public_router
from app.modules.favorites.router import me_router as favorites_me_router
from app.modules.favorites.router import public_router as favorites_public_router
from app.modules.leads.router import capture_idempotent_router as leads_capture_idempotent_router
from app.modules.leads.router import capture_router as leads_capture_router
from app.modules.leads.router import portal_router as leads_portal_router
from app.modules.listings.router import portal_router as listings_portal_router
from app.modules.listings.router import public_router as listings_public_router
from app.modules.listings.router import seo_router as listings_seo_router
from app.modules.media.router import router as media_portal_router
from app.modules.notifications.router import me_router as notifications_me_router
from app.modules.notifications.router import ws_router as notifications_ws_router
from app.modules.reviews.router import portal_router as reviews_portal_router
from app.modules.reviews.router import public_router as reviews_public_router
from app.modules.syndication.router import feeds_router as syndication_feeds_router
from app.modules.syndication.router import portal_router as syndication_portal_router
from app.modules.tenants.router import billing_webhook_router as tenants_billing_webhook_router
from app.modules.tenants.router import platform_admin_router as tenants_platform_admin_router
from app.modules.tenants.router import (
    platform_billing_idempotent_router as tenants_platform_billing_idempotent_router,
)
from app.modules.tenants.router import platform_router as tenants_platform_router
from app.modules.tenants.router import site_router as tenants_site_router
from app.modules.tenants.service import DomainTenantResolver
from app.modules.transactions.router import portal_router as transactions_portal_router
from app.modules.users.router import staff_router, users_router
from app.modules.valuations.router import public_router as valuations_public_router
from app.modules.webhooks.router import portal_router as webhooks_portal_router

# Task bodies use `@shared_task`, which binds to Celery's *current* app. Nothing
# else in the API process imports the configured app, so without this import the
# current app is an unconfigured default whose broker_url is None — kombu then
# falls back to AMQP/RabbitMQ on :5672 and every request-path `.delay()` (welcome
# email, media processing, notifications) fails with a connection refusal instead
# of enqueueing to Redis. Worse, the failure is silent from the caller's side:
# the post-commit enqueue happens after the response is sent, so the request
# still returns 201 and the job is simply lost. Importing the module configures
# the app and calls set_current(); `app.workers.db` does the same for the
# worker-thread path (the Part 8 finding).
from app.workers import celery_app as _celery_app  # noqa: F401

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    # A separate long-lived client for the Celery broker: it is a different
    # Redis database (and may be a different instance) than the cache, and both
    # the readiness probe and the queue-depth gauge poll it on a fixed interval
    # — building a pool per probe would churn short-lived connections against
    # the broker exactly when it is already struggling.
    app.state.broker_redis = Redis.from_url(settings.celery_broker_url, decode_responses=True)
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.storage = create_storage(settings)
    app.state.tenant_resolver = DomainTenantResolver(
        session_factory=app.state.session_factory,
        redis=app.state.redis,
        cache_ttl_seconds=settings.tenant_cache_ttl_seconds,
    )
    # OTEL instrumentation needs the live engine, so it happens here rather
    # than in the factory; both calls no-op when tracing is off (§14).
    instrument_app(app, app.state.engine, settings)
    logger.info("app_startup", env=settings.app_env)
    yield
    await app.state.engine.dispose()
    await app.state.redis.aclose()
    await app.state.broker_redis.aclose()
    logger.info("app_shutdown")


def build_api_v1_router() -> APIRouter:
    """All module routers mount here as parts land (tenants, auth, listings, ...)."""
    router = APIRouter(prefix="/api/v1")
    router.include_router(tenants_platform_router)
    router.include_router(tenants_platform_billing_idempotent_router)
    router.include_router(tenants_platform_admin_router)
    router.include_router(tenants_billing_webhook_router)
    router.include_router(tenants_site_router)
    router.include_router(auth_router)
    router.include_router(platform_auth_router)
    router.include_router(users_router)
    router.include_router(staff_router)
    router.include_router(listings_public_router)
    router.include_router(listings_portal_router)
    router.include_router(listings_seo_router)
    router.include_router(media_portal_router)
    router.include_router(leads_capture_router)
    router.include_router(leads_capture_idempotent_router)
    router.include_router(leads_portal_router)
    router.include_router(agents_public_router)
    router.include_router(agents_portal_router)
    router.include_router(teams_router)
    router.include_router(favorites_me_router)
    router.include_router(favorites_public_router)
    router.include_router(appointments_public_router)
    router.include_router(appointments_booking_idempotent_router)
    router.include_router(appointments_me_router)
    router.include_router(appointments_portal_router)
    router.include_router(valuations_public_router)
    router.include_router(content_public_router)
    router.include_router(content_portal_router)
    router.include_router(blog_public_router)
    router.include_router(blog_portal_router)
    router.include_router(reviews_public_router)
    router.include_router(reviews_portal_router)
    router.include_router(notifications_me_router)
    router.include_router(notifications_ws_router)
    router.include_router(transactions_portal_router)
    router.include_router(syndication_feeds_router)
    router.include_router(syndication_portal_router)
    router.include_router(webhooks_portal_router)
    router.include_router(analytics_public_router)
    router.include_router(analytics_portal_router)
    router.include_router(analytics_listing_report_router)
    router.include_router(compliance_public_router)
    router.include_router(compliance_site_router)
    router.include_router(compliance_me_router)
    router.include_router(compliance_portal_router)
    return router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)
    # Telemetry first: an exception during the rest of startup should already be
    # captured. Both are no-ops without a DSN / with tracing disabled (§14).
    init_sentry(settings, component="api")
    init_tracing(settings)

    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.state.settings = settings

    # Middleware executes in reverse add-order on requests: context → metrics
    # → global rate limit → CORS → security headers → tenant resolution →
    # routes.
    #
    # Metrics sits directly inside the context layer so its latency histogram
    # covers everything the access log's duration_ms does (§14) — including a
    # tenant-resolution 404, which is real user-visible latency.
    #
    # The global limiter sits above CORS and tenant resolution so a flood
    # costs one Redis lookup rather than a tenant lookup plus routing; CORS in
    # turn sits above tenant resolution because a preflight carries no
    # credentials and must be answerable for a host the resolver would reject.
    # Security headers wrap the tenant layer so a 404/402 problem response
    # carries them too.
    app.add_middleware(TenantResolutionMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(TenantCORSMiddleware, settings=settings)
    if settings.global_rate_limit_enabled:
        app.add_middleware(GlobalRateLimitMiddleware, settings=settings)
    # Read once, at construction: the middleware stack is fixed for the app's
    # lifetime, so toggling `metrics_enabled` on a live app only closes the
    # scrape endpoint (which re-reads it per request) — collection keeps
    # running. Flipping it is a restart-scoped operation.
    if settings.metrics_enabled:
        app.add_middleware(MetricsMiddleware)
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(internal_router)
    app.include_router(build_api_v1_router())

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        openapi_schema = get_openapi(
            title=app.title,
            version="1.0.0",
            description="Real Estate Multi-Tenant API",
            routes=app.routes,
        )
        openapi_schema["components"]["securitySchemes"] = {
            "HTTPBearer": {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "Enter JWT Bearer token (without 'Bearer ' prefix)."
            }
        }
        openapi_schema["security"] = [{"HTTPBearer": []}]
        app.openapi_schema = openapi_schema
        return app.openapi_schema

    app.openapi = custom_openapi
    return app


app = create_app()
