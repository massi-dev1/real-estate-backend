"""HTTP layer for auth (§7.1).

- ``auth_router`` — tenant-scoped: register/login/refresh/logout, password
  reset, email verification.
- ``platform_auth_router`` — the same login/refresh/logout for platform staff
  (tenant-exempt; ``tenant_id`` is ``None`` end to end).

The access JWT travels in the response body; the refresh token only ever in an
``httpOnly`` cookie scoped to the auth path — it is never readable by JS and
never sent to non-auth endpoints.
"""

from fastapi import APIRouter, Request, Response, status

from app.core.config import Settings
from app.core.permissions import CurrentUserDep
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


@auth_router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(
    data: RegisterRequest,
    tenant: TenantDep,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> TokenOut:
    issued = await service.register(tenant.id, data, client_info(request))
    return _token_response(issued, request, response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/login")
async def login(
    data: LoginRequest,
    tenant: TenantDep,
    service: AuthServiceDep,
    request: Request,
    response: Response,
) -> TokenOut:
    issued = await service.login(tenant.id, data, client_info(request))
    return _token_response(issued, request, response, cookie_path=TENANT_AUTH_PATH)


@auth_router.post("/refresh")
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


@auth_router.post("/password/forgot", status_code=status.HTTP_202_ACCEPTED)
async def forgot_password(
    data: ForgotPasswordRequest, tenant: TenantDep, service: AuthServiceDep
) -> AcceptedOut:
    await service.forgot_password(tenant.id, data.email)
    return AcceptedOut(detail="If an account exists for this email, a reset code has been sent.")


@auth_router.post("/password/reset", status_code=status.HTTP_204_NO_CONTENT)
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
