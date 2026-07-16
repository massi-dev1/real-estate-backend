"""HTTP layer for the users module.

- ``users_router`` — tenant-scoped: self profile (``/users/me``) and tenant-admin
  user management (RBAC-guarded, §7.2).
- ``staff_router`` — platform back-office staff accounts (tenant-exempt).
"""

import uuid

from fastapi import APIRouter, Depends, Query, status

from app.core.pagination import MAX_PAGE_SIZE, Page
from app.core.permissions import AuthenticatedUser, CurrentUserDep, Permission, require
from app.modules.auth.service import AuthServiceDep
from app.modules.users.schemas import (
    PlatformStaffCreate,
    ProfileUpdate,
    UserAdminUpdate,
    UserCreate,
    UserOut,
)
from app.modules.users.service import UserServiceDep

users_router = APIRouter(prefix="/users", tags=["users"])


@users_router.get("/me")
async def get_my_profile(user: CurrentUserDep, service: UserServiceDep) -> UserOut:
    return UserOut.model_validate(await service.get(user.tenant_id, user.id))


@users_router.patch("/me")
async def update_my_profile(
    data: ProfileUpdate, user: CurrentUserDep, service: UserServiceDep
) -> UserOut:
    return UserOut.model_validate(await service.update_profile(user.tenant_id, user.id, data))


@users_router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    data: UserCreate,
    service: UserServiceDep,
    admin: AuthenticatedUser = Depends(require(Permission.USER_MANAGE)),
) -> UserOut:
    user = await service.create_account(
        admin.tenant_id,
        email=data.email,
        password=data.password,
        role=data.role,
        first_name=data.first_name,
        last_name=data.last_name,
        locale=data.locale,
        phone=data.phone,
    )
    return UserOut.model_validate(user)


@users_router.get("")
async def list_users(
    service: UserServiceDep,
    admin: AuthenticatedUser = Depends(require(Permission.USER_VIEW)),
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[UserOut]:
    items, next_cursor, total = await service.list(admin.tenant_id, cursor=cursor, limit=limit)
    return Page(
        items=[UserOut.model_validate(u) for u in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )


@users_router.get("/{user_id}")
async def get_user(
    user_id: uuid.UUID,
    service: UserServiceDep,
    admin: AuthenticatedUser = Depends(require(Permission.USER_VIEW)),
) -> UserOut:
    return UserOut.model_validate(await service.get(admin.tenant_id, user_id))


@users_router.patch("/{user_id}")
async def update_user(
    user_id: uuid.UUID,
    data: UserAdminUpdate,
    service: UserServiceDep,
    auth: AuthServiceDep,
    admin: AuthenticatedUser = Depends(require(Permission.USER_MANAGE)),
) -> UserOut:
    assert admin.tenant_id is not None  # tenant route: platform tokens rejected upstream
    user = await service.admin_update(admin.tenant_id, user_id, data, actor_id=admin.id)
    # Disable/demotion must bite now, not when the 15-min token expires; the
    # users service can't call auth (auth already depends on it), so the
    # router orchestrates the two services.
    fields = data.model_fields_set
    if "role" in fields or ("status" in fields and data.status != "active"):
        await auth.force_logout_user(admin.tenant_id, user_id)
    return UserOut.model_validate(user)


@users_router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: uuid.UUID,
    service: UserServiceDep,
    auth: AuthServiceDep,
    admin: AuthenticatedUser = Depends(require(Permission.USER_MANAGE)),
) -> None:
    assert admin.tenant_id is not None
    await service.soft_delete(admin.tenant_id, user_id, actor_id=admin.id)
    await auth.force_logout_user(admin.tenant_id, user_id)


staff_router = APIRouter(
    prefix="/platform/staff",
    tags=["platform:staff"],
    dependencies=[Depends(require(Permission.PLATFORM_STAFF_MANAGE))],
)


@staff_router.post("", status_code=status.HTTP_201_CREATED)
async def create_platform_staff(data: PlatformStaffCreate, service: UserServiceDep) -> UserOut:
    return UserOut.model_validate(await service.create_platform_staff(data))


@staff_router.get("")
async def list_platform_staff(
    service: UserServiceDep,
    cursor: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
) -> Page[UserOut]:
    items, next_cursor, total = await service.list(None, cursor=cursor, limit=limit)
    return Page(
        items=[UserOut.model_validate(u) for u in items],
        next_cursor=next_cursor,
        total_estimate=total,
    )
