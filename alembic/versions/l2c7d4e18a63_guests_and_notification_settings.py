"""guests_and_notification_settings: имена гостей в записи компании и выключатели колокольчика

Revision ID: l2c7d4e18a63
Revises: k9f2b6c31e52
Create Date: 2026-10-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l2c7d4e18a63"
down_revision: Union[str, Sequence[str], None] = "k9f2b6c31e52"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SWITCHES = ("notify_booking_created", "notify_booking_cancelled", "notify_booking_rescheduled")


def upgrade() -> None:
    op.add_column("appointments", sa.Column(
        "guest_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb"),
    ))
    for name in SWITCHES:
        op.add_column("memberships", sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    for name in reversed(SWITCHES):
        op.drop_column("memberships", name)
    op.drop_column("appointments", "guest_names")
