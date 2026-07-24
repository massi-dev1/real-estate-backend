"""Application settings loaded from environment (pydantic-settings).

Required values (APP_SECRET_KEY, DATABASE_URL, ...) have no defaults on
purpose: the app must fail fast at startup when configuration is missing.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "staging", "production"] = "local"
    app_debug: bool = False
    app_name: str = "Real Estate Platform API"
    app_secret_key: str = Field(min_length=32)

    # Runtime connection uses the non-superuser role so Postgres RLS applies;
    # DDL (Alembic migrations) runs as the owner role.
    database_url: str
    database_ddl_url: str

    redis_url: str = "redis://localhost:6379/0"

    # Host → tenant lookups are cached in Redis for this long (§4.1).
    tenant_cache_ttl_seconds: int = 300

    # Auth (§7.1): short-lived access JWT + rotating refresh token in a cookie.
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_days: int = 30
    password_reset_ttl_seconds: int = 1800
    email_verification_ttl_seconds: int = 86400

    smtp_host: str = "localhost"
    smtp_port: int = 1025
    email_from: str = "no-reply@realestate.local"

    # Celery (§12): one Redis instance also serves as broker + result backend.
    # A dedicated DB index keeps task keys out of the cache/session keyspace.
    celery_broker_url: str = "redis://localhost:6379/2"
    celery_result_backend: str = "redis://localhost:6379/2"
    # A published listing past this age is flagged stale for agent review (§8.1).
    listing_stale_after_days: int = 90
    # Unassigned leads past this age get an admin-notifying escalation
    # activity from the Beat sweep (§8.4) — never auto-reassigned.
    lead_escalation_minutes: int = 30

    # Object storage (§8.2): S3-compatible, MinIO locally. Two buckets —
    # `media` holds processed public variants (served via CDN in prod),
    # `docs` holds originals + private documents (presigned access only).
    storage_endpoint_url: str = "http://localhost:9000"
    # No defaults on purpose (same fail-fast rule as APP_SECRET_KEY): a prod
    # deploy missing these must not silently sign URLs with dev credentials.
    storage_access_key: str
    storage_secret_key: str
    storage_region: str = "us-east-1"
    storage_media_bucket: str = "media"
    storage_docs_bucket: str = "media-private"
    # CDN base for public variant URLs; empty = serve from the endpoint itself.
    media_public_base_url: str = ""
    media_upload_url_ttl_seconds: int = 900
    media_download_url_ttl_seconds: int = 900  # §8.2: presigned GET, 15 min
    media_max_upload_bytes: int = 25 * 1024 * 1024
    # Default photo quota per listing; tenants override via settings.media.*.
    media_max_photos_per_listing: int = 50

    # Tenant lifecycle & billing (§8.16). No live payment provider in this
    # environment — the sandbox "stub" provider is the default (design the seam,
    # defer the live integration). ``billing_webhook_secret`` signs/verifies
    # webhooks (§10.9); it has a dev default since the stub is self-contained.
    billing_provider: str = "stub"
    billing_webhook_secret: str = "dev-billing-webhook-secret"
    trial_length_days: int = 14
    # Dunning (§8.16): a past_due subscription stays reachable this long before
    # the dunning sweep auto-suspends the tenant.
    billing_grace_days: int = 7
    # Offboard (§8.16): an offboarded tenant's data is exported then purged this
    # many days later (a window to undo an accidental offboard).
    offboard_deletion_delay_days: int = 30
    # A short-lived, single-use impersonation access token (§8.16/§10.11).
    impersonation_token_ttl_seconds: int = 900

    # AI features (§8.18): a provider-agnostic seam (``integrations/ai/``). No
    # real model credentials in this environment, so the default provider is the
    # offline template ``stub`` (design the seam, defer the tuned product) — the
    # API surface stays stable while the implementation improves. Set
    # ``ai_provider=anthropic`` + ``ai_api_key`` to route through a live model.
    ai_provider: str = "stub"
    ai_api_key: str = ""
    ai_model: str = "claude-opus-4-8"
    # Request-time generation is a >200ms external call (§8.18): a sane timeout
    # + graceful error, never a hang. A provider failure becomes a 503, not a 500.
    ai_timeout_seconds: float = 30.0
    ai_max_output_tokens: int = 1024

    # Observability (§14). Every exporter is opt-in and offline-safe: an empty
    # DSN / disabled flag turns the feature off, so the app boots with no
    # telemetry credentials (same stance as the AI/billing stubs).
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.0
    # Tags every Sentry event with the deployed revision (set from the image
    # tag / git SHA in CI). Empty = let Sentry infer.
    sentry_release: str = ""
    otel_enabled: bool = False
    otel_exporter_endpoint: str = ""
    otel_service_name: str = "real-estate-backend"
    # Prometheus scraping (§14). On by default — it costs nothing without a
    # scraper and needs no credentials. The endpoint itself is guarded: only
    # reachable with ``metrics_auth_token`` (when set) or from the proxy's
    # private network, never public.
    metrics_enabled: bool = True
    metrics_auth_token: str = ""

    # Additive platform/admin allowlist on top of the dynamic per-tenant
    # allowlist built from `tenant_domains` (§10.1) — the back-office SPA and
    # local dev frontends live here, agency sites resolve themselves.
    cors_origins: str = ""

    # Edge hardening (§10.1). HSTS is only meaningful over TLS, so it is sent in
    # staging/production (where Caddy terminates TLS) or on a request that
    # arrived over https — never on plain-http local dev, where a cached
    # max-age would make localhost unreachable over http for a year.
    hsts_max_age_seconds: int = 31_536_000  # 1 year
    hsts_include_subdomains: bool = True

    # Layered rate limits (§10.2). The global per-IP budget is a coarse
    # backstop in front of everything; per-endpoint auth limits are much
    # tighter. Both degrade open when Redis is unavailable.
    global_rate_limit_enabled: bool = True
    global_rate_limit_per_minute: int = 300
    auth_rate_limit_per_minute: int = 10

    # Field-level encryption (§10.7): AES-GCM secrets-at-rest for reversible
    # values (MFA TOTP secrets, Part 29; future provider tokens). No default —
    # same fail-fast rule as APP_SECRET_KEY, and deliberately a *different*
    # key so rotating one never touches the other. ``field_encryption_key_id``
    # names the *current* key for new ciphertext; ``field_encryption_keys``
    # (JSON map of id -> key) adds prior key ids so already-encrypted rows
    # keep decrypting through a rotation — empty means "current key only".
    field_encryption_key: str = Field(min_length=32)
    field_encryption_key_id: str = "v1"
    field_encryption_keys: str = ""

    # Idempotency-Key (§9): replay a POST's cached response instead of
    # re-executing it. 24h covers a client retrying well after a timeout.
    idempotency_key_ttl_seconds: int = 86_400

    # RFC 9457 problem `type` values are built as f"{problem_type_base}{slug}".
    problem_type_base: str = "https://api.realestate.example/errors/"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def is_local(self) -> bool:
        return self.app_env == "local"


@lru_cache
def get_settings() -> Settings:
    return Settings()
