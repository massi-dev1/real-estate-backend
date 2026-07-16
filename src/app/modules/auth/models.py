"""Refresh-token sessions (§6.1/§7.1): rotating tokens, one row per token.

Rotation keeps every token of a login chain in the same ``family_id``. A
revoked row being presented again is the theft signal that revokes the whole
family. Like ``users``, the table carries a nullable ``tenant_id`` (platform
staff sessions) and sits under the identity-RLS policy.
"""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, String
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
