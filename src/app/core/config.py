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

    # Interim auth for /api/v1/platform/* until platform-staff RBAC lands (Part 3).
    platform_api_key: str = Field(min_length=16)

    # Host → tenant lookups are cached in Redis for this long (§4.1).
    tenant_cache_ttl_seconds: int = 300

    smtp_host: str = "localhost"
    smtp_port: int = 1025

    cors_origins: str = ""

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
