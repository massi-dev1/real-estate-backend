"""HTTP layer for auth (§7.1).

- ``auth_router`` — tenant-scoped: register/login/refresh/logout, password
  reset, email verification.
- ``platform_auth_router`` — the same login/refresh/logout for platform staff
  (tenant-exempt; ``tenant_id`` is ``None`` end to end).

The access JWT travels in the response body; the refresh token only ever in an
``httpOnly`` cookie scoped to the auth path — it is never readable by JS and
never sent to non-auth endpoints.
"""

from fastapi import APIRouter, Depends, Request, Response, status

from app.core.config import Settings, get_settings
from app.core.permissions import CurrentUserDep
from app.core.rate_limit import auth_rate_limit
from app.core.tenancy import TenantDep
from app.modules.auth.schemas import (
    AcceptedOut,
    AuthUserOut,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenOut,
    VerifyEmailRequest,
)
from app.modules.auth.service import AuthServiceDep, IssuedTokens, client_info

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
) -> TokenOut:
    issued = await service.login(tenant.id, data, client_info(request))
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


platform_auth_router = APIRouter(prefix="/platform/auth", tags=["platform:auth"])
# No per-endpoint limit here: this router is tenant-exempt, and every limit in
# `core.rate_limit` is keyed on tenant + IP. Platform staff login is covered by
# the global per-IP budget; a tenant-free auth limit would need its own key
# scheme, which is worth adding alongside Part 29's account lockout rather than
# inventing a second keying convention now.


@platform_auth_router.post("/login")
async def platform_login(
    data: LoginRequest, service: AuthServiceDep, request: Request, response: Response
) -> TokenOut:
    issued = await service.login(None, data, client_info(request))
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
