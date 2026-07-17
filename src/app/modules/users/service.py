"""User business logic: the identity store for tenant users and platform staff.

Password hashes never leave this module — the auth module verifies credentials
through :meth:`UserService.verify_credentials` and receives a
:class:`UserIdentity` DTO, not the ORM row (module-boundary rule §5).
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends
from sqlalchemy.exc import IntegrityError

from app.core.database import SessionDep
from app.core.exceptions import ConflictError, NotFoundError
from app.core.pagination import InvalidCursorError, clamp_limit, decode_cursor, encode_cursor
from app.core.permissions import PLATFORM_ROLES, Role
from app.core.security import DUMMY_PASSWORD_HASH, hash_password, verify_password
from app.modules.users.models import User, UserStatus
from app.modules.users.repository import UserRepository
from app.modules.users.schemas import PlatformStaffCreate, ProfileUpdate, UserAdminUpdate


@dataclass(frozen=True, slots=True)
class UserIdentity:
    """What other modules (auth) get to know about a user."""

    id: uuid.UUID
    tenant_id: uuid.UUID | None
    email: str
    role: Role
    locale: str
    email_verified_at: datetime | None


def _to_identity(user: User) -> UserIdentity:
    return UserIdentity(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        role=user.role,
        locale=user.locale,
        email_verified_at=user.email_verified_at,
    )


class UserService:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    async def _flush_or_conflict(self) -> None:
        """The unique (tenant_id, email) constraint is the real guard against
        duplicate races; surface its violation as 409, not 500."""
        try:
            await self.repo.flush()
        except IntegrityError as exc:
            raise ConflictError("An account with this email already exists.") from exc

    async def _get_or_404(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID) -> User:
        user = await self.repo.get(tenant_id, user_id)
        if user is None:
            raise NotFoundError("User not found.")
        return user

    async def create_account(
        self,
        tenant_id: uuid.UUID | None,
        *,
        email: str,
        password: str,
        role: Role,
        first_name: str | None = None,
        last_name: str | None = None,
        locale: str = "fr",
        phone: str | None = None,
    ) -> User:
        if (tenant_id is None) != (role in PLATFORM_ROLES):
            raise ConflictError("The role does not match the account scope.")
        if await self.repo.get_by_email(tenant_id, email) is not None:
            raise ConflictError("An account with this email already exists.")
        user = User(
            tenant_id=tenant_id,
            email=email,
            password_hash=hash_password(password),
            role=role,
            first_name=first_name,
            last_name=last_name,
            locale=locale,
            phone=phone,
        )
        self.repo.add(user)
        await self._flush_or_conflict()
        return user

    async def register_account(
        self,
        tenant_id: uuid.UUID,
        *,
        email: str,
        password: str,
        role: Role,
        first_name: str | None = None,
        last_name: str | None = None,
        locale: str = "fr",
        phone: str | None = None,
    ) -> UserIdentity:
        """Self-registration entry point used by the auth module — returns the
        identity DTO so ORM rows never cross the module boundary."""
        user = await self.create_account(
            tenant_id,
            email=email,
            password=password,
            role=role,
            first_name=first_name,
            last_name=last_name,
            locale=locale,
            phone=phone,
        )
        return _to_identity(user)

    async def create_platform_staff(self, data: PlatformStaffCreate) -> User:
        return await self.create_account(
            None,
            email=data.email,
            password=data.password,
            role=data.role,
            first_name=data.first_name,
            last_name=data.last_name,
        )

    async def get(self, tenant_id: uuid.UUID | None, user_id: uuid.UUID) -> User:
        return await self._get_or_404(tenant_id, user_id)

    # Defined before ``list`` on purpose: in the class body below that point,
    # the bare name ``list`` resolves to the method, not the builtin, which
    # breaks ``list[...]`` annotations.
    async def list_active_agents(self, tenant_id: uuid.UUID) -> list[UserIdentity]:
        """Active AGENT-role tenant users — the round-robin assignment pool."""
        agents = await self.repo.list_active_by_role(tenant_id, Role.AGENT)
        return [_to_identity(u) for u in agents]

    async def list(
        self, tenant_id: uuid.UUID | None, *, cursor: str | None, limit: int | None
    ) -> tuple[list[User], str | None, int]:
        page_size = clamp_limit(limit)
        after: tuple[datetime, uuid.UUID] | None = None
        if cursor is not None:
            values = decode_cursor(cursor)
            try:
                after = (
                    datetime.fromisoformat(values["created_at"]),
                    uuid.UUID(values["id"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidCursorError("The provided cursor is malformed.") from exc

        rows = await self.repo.list_page(tenant_id, after=after, limit=page_size)
        items = rows[:page_size]
        next_cursor = None
        if len(rows) > page_size:
            last = items[-1]
            next_cursor = encode_cursor(
                {"created_at": last.created_at.isoformat(), "id": str(last.id)}
            )
        total = await self.repo.count(tenant_id)
        return items, next_cursor, total

    async def update_profile(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, data: ProfileUpdate
    ) -> User:
        user = await self._get_or_404(tenant_id, user_id)
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        await self.repo.flush()
        return user

    async def admin_update(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        data: UserAdminUpdate,
        *,
        actor_id: uuid.UUID,
    ) -> User:
        user = await self._get_or_404(tenant_id, user_id)
        patch = data.model_dump(exclude_unset=True)
        if user_id == actor_id and ("role" in patch or "status" in patch):
            # An admin demoting/disabling themselves is a lockout footgun.
            raise ConflictError("You cannot change your own role or status.")
        if "status" in patch:
            # Refresh re-checks status via get_identity_if_active; live access
            # tokens are force-revoked by the router through the auth service.
            patch["status"] = UserStatus(patch["status"])
        for field, value in patch.items():
            setattr(user, field, value)
        await self.repo.flush()
        return user

    async def soft_delete(
        self, tenant_id: uuid.UUID, user_id: uuid.UUID, *, actor_id: uuid.UUID
    ) -> None:
        if user_id == actor_id:
            raise ConflictError("You cannot delete your own account.")
        user = await self._get_or_404(tenant_id, user_id)
        user.deleted_at = datetime.now(UTC)
        await self.repo.flush()

    # ---- identity API consumed by the auth module ----

    async def verify_credentials(
        self, tenant_id: uuid.UUID | None, email: str, password: str
    ) -> UserIdentity | None:
        """Argon2-verify and return the identity, or ``None`` — for a wrong
        password, an unknown email, or a disabled account alike. Records
        ``last_login_at`` on success."""
        user = await self.repo.get_by_email(tenant_id, email)
        if user is None:
            # Same Argon2 cost as a real check → no enumeration via timing.
            verify_password(password, DUMMY_PASSWORD_HASH)
            return None
        if not verify_password(password, user.password_hash):
            return None
        if user.status is not UserStatus.ACTIVE:
            return None
        user.last_login_at = datetime.now(UTC)
        await self.repo.flush()
        return _to_identity(user)

    async def get_identity_if_active(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> UserIdentity | None:
        user = await self.repo.get(tenant_id, user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            return None
        return _to_identity(user)

    async def get_identity_by_email(
        self, tenant_id: uuid.UUID | None, email: str
    ) -> UserIdentity | None:
        user = await self.repo.get_by_email(tenant_id, email)
        if user is None or user.status is not UserStatus.ACTIVE:
            return None
        return _to_identity(user)

    async def set_password(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID, new_password: str
    ) -> UserIdentity | None:
        user = await self.repo.get(tenant_id, user_id)
        if user is None or user.status is not UserStatus.ACTIVE:
            return None
        user.password_hash = hash_password(new_password)
        await self.repo.flush()
        return _to_identity(user)

    async def mark_email_verified(
        self, tenant_id: uuid.UUID | None, user_id: uuid.UUID
    ) -> UserIdentity | None:
        user = await self.repo.get(tenant_id, user_id)
        if user is None:
            return None
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(UTC)
            await self.repo.flush()
        return _to_identity(user)


def get_user_service(session: SessionDep) -> UserService:
    return UserService(UserRepository(session))


UserServiceDep = Annotated[UserService, Depends(get_user_service)]
