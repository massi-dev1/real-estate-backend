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
    mfa_enabled: bool = False


class TokenOut(OutSchema):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: AuthUserOut


class MfaRequiredOut(OutSchema):
    """The 'password accepted, second factor still owed' response (§7.1).

    Deliberately a distinct shape from :class:`TokenOut` — no access token, no
    refresh cookie, and ``mfaRequired`` as an unambiguous discriminator so a
    client cannot mistake a half-finished login for a complete one. The
    ``mfaToken`` is a short-lived ticket that grants exactly one thing: the
    right to present a code at ``/auth/mfa/verify``.
    """

    mfa_required: bool = True
    mfa_token: str
    expires_in: int


class AcceptedOut(OutSchema):
    """202 envelope for fire-and-forget flows (password reset, verification)."""

    detail: str


# ---- MFA (§7.1) ----

_CODE_FIELD = Field(min_length=6, max_length=10)


class MfaVerifyRequest(InputSchema):
    """Second step of a login that demanded a factor."""

    mfa_token: str = Field(min_length=16, max_length=256)
    code: str = _CODE_FIELD


class MfaConfirmRequest(InputSchema):
    """Proves the authenticator app holds the freshly-minted secret."""

    code: str = _CODE_FIELD


class MfaDisableRequest(InputSchema):
    """Re-authentication before stripping the second factor: a stolen session
    must not be able to remove the control that protects the account."""

    password: str = Field(min_length=1, max_length=128)


class MfaEnrolmentOut(OutSchema):
    """The provisioning URI the client renders as a QR code.

    The raw ``secret`` is included for manual entry (every authenticator app
    offers it as a fallback) — this is the one response that carries it, it is
    only ever returned to the already-authenticated owner, and it becomes
    useless the moment enrolment is confirmed or replaced.
    """

    provisioning_uri: str
    secret: str


class MfaStatusOut(OutSchema):
    enabled: bool
    enrolled_at: datetime | None


# ---- Sessions (§10.3) ----


class SessionOut(OutSchema):
    """One live sign-in. Carries no token material — only what a person needs
    to recognise a device and decide whether to kill it."""

    id: uuid.UUID
    user_agent: str | None
    ip: str | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime
    current: bool


# ---- OAuth (§7.1, seam) ----


class OAuthProvidersOut(OutSchema):
    """Which social buttons the frontend should render. Empty until a provider
    has credentials — the seam is wired, the integration is credential-gated."""

    providers: list[str]


class OAuthStartOut(OutSchema):
    authorization_url: str
    state: str


class OAuthCallbackRequest(InputSchema):
    code: str = Field(min_length=1, max_length=2048)
    state: str = Field(min_length=1, max_length=256)
