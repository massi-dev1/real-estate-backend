"""User accounts (§6.1) — one table for tenant users *and* platform staff.

``tenant_id`` is NULL exactly for platform-staff rows (``platform_admin`` /
``platform_support``, §7.2). The table is under the identity-RLS policy
(``app.core.rls.enable_identity_rls_sql``): tenant-scoped sessions see only
their tenant's rows, unscoped (platform) sessions see only the NULL-tenant
rows. Email is unique per tenant, with NULLs *not* distinct so platform staff
emails are unique among themselves too.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedString
from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.core.permissions import Role


class UserStatus(enum.StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "email", postgresql_nulls_not_distinct=True),)

    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    email: Mapped[str] = mapped_column(String(320))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[Role] = mapped_column(
        Enum(
            Role,
            name="user_role",
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        )
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(
            UserStatus,
            name="user_status",
            native_enum=False,
            length=20,
            values_callable=lambda e: [m.value for m in e],
        ),
        default=UserStatus.ACTIVE,
        server_default=UserStatus.ACTIVE.value,
    )
    first_name: Mapped[str | None] = mapped_column(String(80))
    last_name: Mapped[str | None] = mapped_column(String(80))
    locale: Mapped[str] = mapped_column(String(10), default="fr", server_default="fr")
    phone: Mapped[str | None] = mapped_column(String(32))
    # TOTP secret (§7.1). Encrypted at rest via Part 30's AES-GCM field cipher
    # (§10.7) — a DB dump must not hand over working second factors. Reversible
    # on purpose (unlike password_hash): TOTP verification needs the seed back.
    mfa_secret: Mapped[str | None] = mapped_column(EncryptedString(255))
    # A secret alone is not a live factor: enrolment mints one, but only a
    # successful verify flips `mfa_enabled`, so an abandoned enrolment can
    # never lock someone out of their own account.
    mfa_enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    mfa_enrolled_at: Mapped[datetime | None]
    email_verified_at: Mapped[datetime | None]
    last_login_at: Mapped[datetime | None]
    # Soft delete (§6): identity history matters; queries exclude these rows.
    deleted_at: Mapped[datetime | None]
