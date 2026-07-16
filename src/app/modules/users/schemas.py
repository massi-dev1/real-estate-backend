"""Pydantic schemas for the users module (self profile, tenant admin, staff)."""

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.core.permissions import Role
from app.core.schema import InputSchema, OutSchema

# Roles a tenant admin may assign; platform roles are never assignable here.
ADMIN_ASSIGNABLE_ROLES = frozenset(
    {Role.BUYER_RENTER, Role.SELLER, Role.AGENT, Role.TEAM_LEAD, Role.ADMIN, Role.MARKETING}
)

PASSWORD_FIELD = Field(min_length=8, max_length=128)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class UserCreate(InputSchema):
    """Tenant-admin account creation (agents, team leads, back-office)."""

    email: EmailStr = Field(max_length=320)
    password: str = PASSWORD_FIELD
    role: Role
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    locale: str = Field(default="fr", min_length=2, max_length=10)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("role")
    @classmethod
    def assignable_role(cls, value: Role) -> Role:
        if value not in ADMIN_ASSIGNABLE_ROLES:
            raise ValueError("this role cannot be assigned to a tenant user")
        return value


class UserAdminUpdate(InputSchema):
    """Tenant-admin patch: role/status plus profile fields."""

    role: Role | None = None
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, min_length=2, max_length=10)
    phone: str | None = Field(default=None, max_length=32)

    @field_validator("role")
    @classmethod
    def assignable_role(cls, value: Role | None) -> Role | None:
        if value is not None and value not in ADMIN_ASSIGNABLE_ROLES:
            raise ValueError("this role cannot be assigned to a tenant user")
        return value


class ProfileUpdate(InputSchema):
    """Self-service profile patch — no role/status/email here."""

    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    locale: str | None = Field(default=None, min_length=2, max_length=10)
    phone: str | None = Field(default=None, max_length=32)


class PlatformStaffCreate(InputSchema):
    email: EmailStr = Field(max_length=320)
    password: str = PASSWORD_FIELD
    role: Role
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)

    @field_validator("role")
    @classmethod
    def platform_role(cls, value: Role) -> Role:
        if value not in (Role.PLATFORM_ADMIN, Role.PLATFORM_SUPPORT):
            raise ValueError("must be a platform role")
        return value


class UserOut(OutSchema):
    id: uuid.UUID
    email: str
    role: Role
    status: str
    first_name: str | None
    last_name: str | None
    locale: str
    phone: str | None
    email_verified_at: datetime | None
    last_login_at: datetime | None
    created_at: datetime
