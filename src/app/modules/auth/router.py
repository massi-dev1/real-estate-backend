"""HTTP layer for auth (§7.1, §10.3).

- ``auth_router`` — tenant-scoped: register/login/refresh/logout, password
  reset, email verification, MFA enrolment + login challenge, the OAuth seam
  and the session list.
- ``platform_auth_router`` — the same login/refresh/logout for platform staff
  (tenant-exempt; ``tenant_id`` is ``None`` end to end).

The access JWT travels in the response body; the refresh token only ever in an
``httpOnly`` cookie scoped to the auth path — it is never readable by JS and
never sent to non-auth endpoints.
"""

import uuid

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.permissions import CurrentUserDep
from app.core.rate_limit import auth_rate_limit
from app.core.tenancy import TenantDep
from app.integrations.auth_oauth.registry import configured_oauth_providers
from app.modules.auth.schemas import (
    AcceptedOut,
    AuthUserOut,
    ForgotPasswordRequest,
    LoginRequest,
    MfaConfirmRequest,
    MfaDisableRequest,
    MfaEnrolmentOut,
    MfaRequiredOut,
    MfaStatusOut,
    MfaVerifyRequest,
    OAuthCallbackRequest,
    OAuthProvidersOut,
    OAuthStartOut,
    RegisterRequest,
    ResetPasswordRequest,
    SessionOut,
    TokenOut,
    VerifyEmailRequest,
)
from app.modules.auth.service import AuthServiceDep, IssuedTokens, MfaChallenge, client_info

REFRESH_COOKIE = "refresh_token"
TENANT_AUTH_PATH = "/api/v1/auth"
PLATFORM_AUTH_PATH = "/api/v1/platform/auth"


def _set_refresh_cookie(
    response: Response, token: str, settings: Settings, *, cookie_path: str
) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=cookie_path,
        httponly=True,
        secure=not settings.is_local,
        samesite="lax",
    )


def _clear_refresh_cookie(response: Response, *, cookie_path: str) -> None:
    response.delete_cookie(REFRESH_COOKIE, path=cookie_path)


def _token_response(
    issued: IssuedTokens, request: Request, response: Response, *, cookie_path: str
) -> TokenOut:
    settings: Settings = request.app.state.settings
    _set_refresh_cookie(response, issued.refresh_token, settings, cookie_path=cookie_path)
    return TokenOut(
        access_token=issued.access_token,
        expires_in=settings.access_token_ttl_seconds,
        user=AuthUserOut.model_validate(issued.user),
    )


# Login answers with *either* a full session or an MFA challenge. Declared as a
# union (most-specific first) rather than annotating the broader type: FastAPI
# coerces a response to its declared model, so a single-type annotation would
# silently strip the other shape's fields — the Part 19 `DealOut` gotcha.
LoginResponse = TokenOut | MfaRequiredOut

auth_router = APIRouter(prefix="/auth", tags=["auth"])

# Per-endpoint limits on the credential-handling routes (§10.2). The budget is
# read once at import — these are process-lifetime route definitions, and the
# knob is a deploy-time setting, not something toggled on a live app.
_AUTH_LIMIT = get_settings().auth_rate_limit_per_minute
_login_limit = Depends(auth_rate_limit("login", _AUTH_LIMIT))
_register_limit = Depends(auth_rate_limit("register", _AUTH_LIMIT))
# Refresh is a legitimate every-15-minutes call for an active session and may
# be issued by several tabs at once, so it gets a roomier budget than login.
_refresh_limit = Depends(auth_rate_limit("refresh", _AUTH_LIMIT * 3))
# Password reset mails a third party, so a tight budget also protects the
# inbox of whoever's address is being submitted.
_reset_limit = Depends(auth_rate_limit("password-reset", _AUTH_LIMIT))
# The MFA verify step is the one endpoint where a six-digit code is guessable
# in principle. The pending ticket is already single-use (one guess per
# ticket, service-side), so this budget bounds how fast a caller can cycle
# *tickets* — belt and braces on top of that.
_mfa_limit = Depends(auth_rate_limit("mfa-verify", _AUTH_LIMIT))


@auth_router.post("/register", status_code=status.HTTP_201_CREATED, dependencies=[_register_limit])
async def register(
    data: RegisterRequest,
    tenant: TenantDep,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> TokenOut:
    issued = await service.register(tenant.id, data, client_info(request))
    return _token_response(issued, request, response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/login", dependencies=[_login_limit])
async def login(
    data: LoginRequest,
    tenant: TenantDep,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> LoginResponse:
    result = await service.login(tenant.id, data, client_info(request))
    if isinstance(result, MfaChallenge):
        # No refresh cookie and no access token: the password step alone is
        # not a session (§7.1).
        return MfaRequiredOut(mfa_token=result.mfa_token, expires_in=result.expires_in)
    return _token_response(result, request, response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/mfa/verify", dependencies=[_mfa_limit])
async def verify_mfa(
    data: MfaVerifyRequest,
    tenant: TenantDep,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> TokenOut:
    issued = await service.verify_mfa(tenant.id, data.mfa_token, data.code, client_info(request))
    return _token_response(issued, request, response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/refresh", dependencies=[_refresh_limit])
async def refresh(
    tenant: TenantDep, service: AuthServiceDep, request: Request, response: Response
) -> TokenOut:
    issued = await service.refresh(
        tenant.id, request.cookies.get(REFRESH_COOKIE), client_info(request)
    )
    return _token_response(issued, request, response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    user: CurrentUserDep, service: AuthServiceDep, request: Request, response: Response
) -> None:
    await service.logout(user.tenant_id, request.cookies.get(REFRESH_COOKIE), user.jti)
    _clear_refresh_cookie(response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(user: CurrentUserDep, service: AuthServiceDep, response: Response) -> None:
    await service.logout_all(user.tenant_id, user.id, user.jti)
    _clear_refresh_cookie(response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post(
    "/password/forgot", status_code=status.HTTP_202_ACCEPTED, dependencies=[_reset_limit]
)
async def forgot_password(
    data: ForgotPasswordRequest, tenant: TenantDep, service: AuthServiceDep
) -> AcceptedOut:
    await service.forgot_password(tenant.id, data.email)
    return AcceptedOut(detail="If an account exists for this email, a reset code has been sent.")


@auth_router.post(
    "/password/reset", status_code=status.HTTP_204_NO_CONTENT, dependencies=[_reset_limit]
)
async def reset_password(
    data: ResetPasswordRequest, tenant: TenantDep, service: AuthServiceDep
) -> None:
    await service.reset_password(tenant.id, data.token, data.new_password)


@auth_router.post("/verify-email/request", status_code=status.HTTP_202_ACCEPTED)
async def request_email_verification(user: CurrentUserDep, service: AuthServiceDep) -> AcceptedOut:
    await service.resend_email_verification(user.tenant_id, user.id)
    return AcceptedOut(detail="A verification code has been sent.")


@auth_router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    data: VerifyEmailRequest, tenant: TenantDep, service: AuthServiceDep
) -> None:
    await service.verify_email(tenant.id, data.token)


# ---- MFA management (§7.1): always the authenticated owner's own factor ----


@auth_router.get("/mfa/status")
async def mfa_status(user: CurrentUserDep, service: AuthServiceDep) -> MfaStatusOut:
    account = await service.users.get(user.tenant_id, user.id)
    return MfaStatusOut(enabled=account.mfa_enabled, enrolled_at=account.mfa_enrolled_at)


@auth_router.post("/mfa/enrol", status_code=status.HTTP_201_CREATED)
async def begin_mfa_enrolment(user: CurrentUserDep, service: AuthServiceDep) -> MfaEnrolmentOut:
    uri, secret = await service.begin_mfa_enrolment(user.tenant_id, user.id)
    return MfaEnrolmentOut(provisioning_uri=uri, secret=secret)


@auth_router.post("/mfa/enrol/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_mfa_enrolment(
    data: MfaConfirmRequest, user: CurrentUserDep, service: AuthServiceDep
) -> None:
    await service.confirm_mfa_enrolment(user.tenant_id, user.id, data.code)


@auth_router.post("/mfa/disable", status_code=status.HTTP_204_NO_CONTENT)
async def disable_mfa(
    data: MfaDisableRequest, user: CurrentUserDep, service: AuthServiceDep
) -> None:
    await service.disable_mfa(user.tenant_id, user.id, data.password)


# ---- Session list / "log out other devices" (§10.3) ----


@auth_router.get("/sessions")
async def list_sessions(
    user: CurrentUserDep, service: AuthServiceDep, request: Request
) -> list[SessionOut]:
    rows = await service.list_sessions(
        user.tenant_id, user.id, presented_token=request.cookies.get(REFRESH_COOKIE)
    )
    return [
        SessionOut(
            id=row.id,
            user_agent=row.user_agent,
            ip=row.ip,
            created_at=row.created_at,
            last_used_at=row.last_used_at,
            expires_at=row.expires_at,
            current=is_current,
        )
        for row, is_current in rows
    ]


@auth_router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_session(
    session_id: uuid.UUID, user: CurrentUserDep, service: AuthServiceDep
) -> None:
    await service.revoke_session(user.tenant_id, user.id, session_id)


# ---- OAuth social login (§7.1) — seam only until credentials exist ----


@auth_router.get("/oauth/providers")
async def list_oauth_providers(tenant: TenantDep, request: Request) -> OAuthProvidersOut:
    """Which social buttons to render. An empty list is the honest answer on a
    deployment with no OAuth credentials — the frontend renders nothing rather
    than a button that 501s when clicked."""
    return OAuthProvidersOut(providers=configured_oauth_providers(request.app.state.settings))


@auth_router.post("/oauth/{provider}/start", dependencies=[_login_limit])
async def start_oauth(provider: str, tenant: TenantDep, service: AuthServiceDep) -> OAuthStartOut:
    url, state = await service.start_oauth(tenant.id, provider)
    return OAuthStartOut(authorization_url=url, state=state)


@auth_router.post("/oauth/{provider}/callback", dependencies=[_login_limit])
async def complete_oauth(
    provider: str,
    data: OAuthCallbackRequest,
    tenant: TenantDep,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> TokenOut:
    issued = await service.complete_oauth(
        tenant.id,
        provider,
        code=data.code,
        state=data.state,
        client=client_info(request),
    )
    return _token_response(issued, request, response, cookie_path=TENANT_AUTH_PATH)


platform_auth_router = APIRouter(prefix="/platform/auth", tags=["platform:auth"])
# No per-endpoint limit here: this router is tenant-exempt, and every limit in
# `core.rate_limit` is keyed on tenant + IP. Platform staff login is covered by
# the global per-IP budget; a tenant-free auth limit would need its own key
# scheme, which is worth adding alongside Part 29's account lockout rather than
# inventing a second keying convention now.


@platform_auth_router.post("/login")
async def platform_login(
    data: LoginRequest, service: AuthServiceDep, request: Request, response: Response
) -> LoginResponse:
    result = await service.login(None, data, client_info(request))
    if isinstance(result, MfaChallenge):
        return MfaRequiredOut(mfa_token=result.mfa_token, expires_in=result.expires_in)
    return _token_response(result, request, response, cookie_path=PLATFORM_AUTH_PATH)


@platform_auth_router.post("/mfa/verify")
async def platform_verify_mfa(
    data: MfaVerifyRequest, service: AuthServiceDep, request: Request, response: Response
) -> TokenOut:
    issued = await service.verify_mfa(None, data.mfa_token, data.code, client_info(request))
    return _token_response(issued, request, response, cookie_path=PLATFORM_AUTH_PATH)


@platform_auth_router.post("/refresh")
async def platform_refresh(
    service: AuthServiceDep, request: Request, response: Response
) -> TokenOut:
    issued = await service.refresh(None, request.cookies.get(REFRESH_COOKIE), client_info(request))
    return _token_response(issued, request, response, cookie_path=PLATFORM_AUTH_PATH)


@platform_auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def platform_logout(
    user: CurrentUserDep, service: AuthServiceDep, request: Request, response: Response
) -> None:
    await service.logout(user.tenant_id, request.cookies.get(REFRESH_COOKIE), user.jti)
    _clear_refresh_cookie(response, cookie_path=PLATFORM_AUTH_PATH)
