"""series_and_waitlist: повторяющиеся записи и лист ожидания

Revision ID: m3d8e5f29b74
Revises: l2c7d4e18a63
Create Date: 2026-10-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "m3d8e5f29b74"
down_revision: Union[str, Sequence[str], None] = "l2c7d4e18a63"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("appointments", sa.Column("series_id", sa.String(length=36), nullable=True))
    op.create_index(op.f("ix_appointments_series_id"), "appointments", ["series_id"], unique=False)
    op.create_table(
        "waitlist_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("service_id", sa.Integer(), nullable=False),
        sa.Column("master_id", sa.Integer(), nullable=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("client_name", sa.String(length=150), nullable=False),
        sa.Column("phone", sa.String(length=20), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=False),
        sa.Column("language", sa.String(length=5), server_default="en", nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["master_id"], ["masters.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_waitlist_entries_id"), "waitlist_entries", ["id"], unique=False)
    op.create_index(op.f("ix_waitlist_entries_organization_id"), "waitlist_entries", ["organization_id"], unique=False)
    op.create_index("ix_waitlist_org_day", "waitlist_entries", ["organization_id", "day"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_waitlist_org_day", table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_organization_id"), table_name="waitlist_entries")
    op.drop_index(op.f("ix_waitlist_entries_id"), table_name="waitlist_entries")
    op.drop_table("waitlist_entries")
    op.drop_index(op.f("ix_appointments_series_id"), table_name="appointments")
    op.drop_column("appointments", "series_id")
