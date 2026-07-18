"""App factory: middleware, exception handlers, router mounting, lifespan."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from redis.asyncio import Redis

from app.core.config import Settings, get_settings
from app.core.database import create_engine, create_session_factory
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware, SecurityHeadersMiddleware
from app.core.storage import create_storage
from app.core.tenancy import TenantResolutionMiddleware
from app.health import router as health_router
from app.modules.agents.router import portal_router as agents_portal_router
from app.modules.agents.router import public_router as agents_public_router
from app.modules.agents.router import teams_router
from app.modules.appointments.router import portal_router as appointments_portal_router
from app.modules.appointments.router import public_router as appointments_public_router
from app.modules.auth.router import auth_router, platform_auth_router
from app.modules.favorites.router import me_router as favorites_me_router
from app.modules.favorites.router import public_router as favorites_public_router
from app.modules.leads.router import capture_router as leads_capture_router
from app.modules.leads.router import portal_router as leads_portal_router
from app.modules.listings.router import portal_router as listings_portal_router
from app.modules.listings.router import public_router as listings_public_router
from app.modules.listings.router import seo_router as listings_seo_router
from app.modules.media.router import router as media_portal_router
from app.modules.tenants.router import platform_router as tenants_platform_router
from app.modules.tenants.router import site_router as tenants_site_router
from app.modules.tenants.service import DomainTenantResolver
from app.modules.users.router import staff_router, users_router
from app.modules.valuations.router import public_router as valuations_public_router

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    app.state.redis = Redis.from_url(settings.redis_url, decode_responses=True)
    app.state.engine = create_engine(settings)
    app.state.session_factory = create_session_factory(app.state.engine)
    app.state.storage = create_storage(settings)
    app.state.tenant_resolver = DomainTenantResolver(
        session_factory=app.state.session_factory,
        redis=app.state.redis,
        cache_ttl_seconds=settings.tenant_cache_ttl_seconds,
    )
    logger.info("app_startup", env=settings.app_env)
    yield
    await app.state.engine.dispose()
    await app.state.redis.aclose()
    logger.info("app_shutdown")


def build_api_v1_router() -> APIRouter:
    """All module routers mount here as parts land (tenants, auth, listings, ...)."""
    router = APIRouter(prefix="/api/v1")
    router.include_router(tenants_platform_router)
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
    router.include_router(leads_portal_router)
    router.include_router(agents_public_router)
    router.include_router(agents_portal_router)
    router.include_router(teams_router)
    router.include_router(favorites_me_router)
    router.include_router(favorites_public_router)
    router.include_router(appointments_public_router)
    router.include_router(appointments_portal_router)
    router.include_router(valuations_public_router)
    return router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url=None,
    )
    app.state.settings = settings

    # Middleware executes in reverse add-order on requests:
    # context → CORS → security headers → tenant resolution → routes.
    app.add_middleware(TenantResolutionMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    if settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origin_list,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(RequestContextMiddleware)

    register_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(build_api_v1_router())
    return app


app = create_app()
