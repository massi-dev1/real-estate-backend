"""Pydantic schemas for the auth module."""

import uuid
from datetime import datetime

from pydantic import EmailStr, Field, field_validator

from app.core.permissions import SELF_REGISTER_ROLES, Role
from app.core.schema import InputSchema, OutSchema

PASSWORD_FIELD = Field(min_length=8, max_length=128)


def _normalize_email(value: str) -> str:
    return value.strip().lower()


class RegisterRequest(InputSchema):
    email: EmailStr = Field(max_length=320)
    password: str = PASSWORD_FIELD
    role: Role = Role.BUYER_RENTER
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
    def self_register_role(cls, value: Role) -> Role:
        if value not in SELF_REGISTER_ROLES:
            raise ValueError("accounts with this role are created by an administrator")
        return value


class LoginRequest(InputSchema):
    email: EmailStr = Field(max_length=320)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class ForgotPasswordRequest(InputSchema):
    email: EmailStr = Field(max_length=320)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return _normalize_email(value)


class ResetPasswordRequest(InputSchema):
    token: str = Field(min_length=16, max_length=128)
    new_password: str = PASSWORD_FIELD


class VerifyEmailRequest(InputSchema):
    token: str = Field(min_length=16, max_length=128)


class AuthUserOut(OutSchema):
    """The token bearer, as returned by login/register/refresh."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    email: str
    role: Role
    locale: str
    email_verified_at: datetime | None


class TokenOut(OutSchema):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserOut


class AcceptedOut(OutSchema):
    """202 envelope for fire-and-forget flows (password reset, verification)."""

    detail: str
