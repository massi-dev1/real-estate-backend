"""Refresh-token sessions and linked OAuth identities (§6.1/§7.1).

Rotation keeps every token of a login chain in the same ``family_id``. A
revoked row being presented again is the theft signal that revokes the whole
family. Like ``users``, both tables carry a nullable ``tenant_id`` (platform
staff rows) and sit under the identity-RLS policy.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AuthSession(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "sessions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    # SHA-256 hex of the opaque refresh token — the token itself is never stored.
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    user_agent: Mapped[str | None] = mapped_column(String(400))
    ip: Mapped[str | None] = mapped_column(String(45))
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    # Set to "now" when the row is issued and again on the presented row each
    # time it is refreshed, so a device's last-active time is real (not just
    # its first sign-in). The session list (§10.3) shows it so a person can
    # tell devices apart — "log out other devices" is only usable then.
    last_used_at: Mapped[datetime | None]


class OAuthIdentity(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A local account linked to an external identity provider (§7.1).

    The unique key is ``(tenant_id, provider, subject)``: the same Google
    account may legitimately hold an account at two different agencies, so the
    external subject is only unique *within* a tenant partition.
    """

    __tablename__ = "oauth_identities"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "subject",
            name="uq_oauth_identities_tenant_provider_subject",
            postgresql_nulls_not_distinct=True,
        ),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(index=True)
    provider: Mapped[str] = mapped_column(String(40))
    # The provider's stable subject id — never the email, which users change.
    subject: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
