"""agent whatsapp number (§8.6 wa.me handoff)

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-17

Adds the E.164 destination number the public WhatsApp handoff uses when the
lead's listing has an assigned agent. Nullable — resolution falls back to the
tenant-level ``settings.contact.whatsapp_number``. ``agent_profiles`` already
carries the fail-closed tenant RLS policy from 0008, so no RLS changes here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_profiles", sa.Column("whatsapp_number", sa.String(length=20), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agent_profiles", "whatsapp_number")
