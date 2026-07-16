"""RBAC: roles, permission constants, the static role → permission matrix and
the ``require()`` route dependency (§7.2).

The matrix lives in code, not the DB — auditable in git, testable. Ownership
scoping ("an agent sees *their* leads") is layered on top inside repositories,
never here.
"""

import enum
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request

from app.core.exceptions import PermissionDeniedError, UnauthorizedError
from app.core.security import decode_access_token, jti_denylist_key
from app.core.tenancy import TenantContext

logger = structlog.get_logger(__name__)


class Role(enum.StrEnum):
    # Tenant-level roles (per-agency accounts).
    BUYER_RENTER = "buyer_renter"
    SELLER = "seller"
    AGENT = "agent"
    TEAM_LEAD = "team_lead"
    ADMIN = "admin"
    MARKETING = "marketing"
    # Platform-level roles: back-office staff, no tenant (users.tenant_id NULL).
    PLATFORM_ADMIN = "platform_admin"
    PLATFORM_SUPPORT = "platform_support"


PLATFORM_ROLES = frozenset({Role.PLATFORM_ADMIN, Role.PLATFORM_SUPPORT})
TENANT_ROLES = frozenset(set(Role) - PLATFORM_ROLES)
# Roles a visitor may self-register as; everything else is granted by an admin.
SELF_REGISTER_ROLES = frozenset({Role.BUYER_RENTER, Role.SELLER})


class Permission(enum.StrEnum):
    # Tenant back-office.
    USER_VIEW = "user:view"
    USER_MANAGE = "user:manage"
    # Platform back-office.
    PLATFORM_TENANT_VIEW = "platform:tenant:view"
    PLATFORM_TENANT_MANAGE = "platform:tenant:manage"
    PLATFORM_STAFF_MANAGE = "platform:staff:manage"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.BUYER_RENTER: frozenset(),
    Role.SELLER: frozenset(),
    Role.AGENT: frozenset(),
    Role.TEAM_LEAD: frozenset(),
    Role.MARKETING: frozenset(),
    Role.ADMIN: frozenset({Permission.USER_VIEW, Permission.USER_MANAGE}),
    Role.PLATFORM_SUPPORT: frozenset({Permission.PLATFORM_TENANT_VIEW}),
    Role.PLATFORM_ADMIN: frozenset(
        {
            Permission.PLATFORM_TENANT_VIEW,
            Permission.PLATFORM_TENANT_MANAGE,
            Permission.PLATFORM_STAFF_MANAGE,
        }
    ),
}


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """The verified bearer of the access token — claims only, no DB row.

    Access tokens live 15 minutes; disable/demote/delete and logout-all
    denylist a user's tracked jtis immediately, and refresh re-checks the DB
    row. ``tenant_id`` is ``None`` exactly when ``role`` is a platform one.
    """

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    role: Role
    jti: str

    def has_permission(self, permission: Permission) -> bool:
        return permission in ROLE_PERMISSIONS[self.role]


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise UnauthorizedError("A bearer access token is required.")
    return token.strip()


async def get_current_user(request: Request) -> AuthenticatedUser:
    """Decode the bearer token and pin it to the request's tenant.

    A token minted for agency A is useless on agency B's domain, and a tenant
    token is useless on platform routes (and vice versa) — the ``tid`` claim
    must match the resolved tenant exactly (§7.1).
    """
    claims = decode_access_token(_bearer_token(request), request.app.state.settings)

    try:
        role = Role(claims.role)
    except ValueError as exc:
        raise UnauthorizedError("The access token is invalid or has expired.") from exc
    if (claims.tenant_id is None) != (role in PLATFORM_ROLES):
        raise UnauthorizedError("The access token is invalid or has expired.")

    tenant: TenantContext | None = getattr(request.state, "tenant", None)
    resolved_tenant_id = tenant.id if tenant is not None else None
    if claims.tenant_id != resolved_tenant_id:
        raise UnauthorizedError("The access token is not valid for this site.")

    # Logout denylists the jti for the token's remaining lifetime. Redis being
    # down degrades to accepting the (still-signed, ≤15 min) token — consistent
    # with the resolver's degrade-don't-fail stance.
    try:
        denied = await request.app.state.redis.exists(jti_denylist_key(claims.jti))
    except Exception:
        logger.warning("jti_denylist_check_failed", jti=claims.jti)
        denied = 0
    if denied:
        raise UnauthorizedError("The access token has been revoked.")

    return AuthenticatedUser(
        id=claims.user_id, tenant_id=claims.tenant_id, role=role, jti=claims.jti
    )


CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]


def require(*permissions: Permission) -> Callable[..., Awaitable[AuthenticatedUser]]:
    """Dependency factory: authenticated user holding *all* given permissions.

    Usage: ``user: AuthenticatedUser = Depends(require(Permission.USER_MANAGE))``
    or ``dependencies=[Depends(require(...))]`` at the router level (§7.2).
    """

    async def _check(user: CurrentUserDep) -> AuthenticatedUser:
        if any(not user.has_permission(p) for p in permissions):
            raise PermissionDeniedError("You do not have permission to perform this action.")
        return user

    return _check
