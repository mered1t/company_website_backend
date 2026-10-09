"""subscriptions: trial, paid_until, payments, platform admin

Revision ID: e7b2c9d41f08
Revises: d4a8f2c61b95
Create Date: 2026-10-08
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7b2c9d41f08"
down_revision: Union[str, Sequence[str], None] = "d4a8f2c61b95"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("is_platform_admin", sa.Boolean(), server_default="false", nullable=False))

    op.add_column("organizations", sa.Column("trial_ends_at", sa.DateTime(), nullable=True))
    op.add_column("organizations", sa.Column("paid_until", sa.DateTime(), nullable=True))
    op.add_column("organizations", sa.Column("is_free", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("organizations", sa.Column("is_blocked", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("organizations", sa.Column("billing_note", sa.Text(), nullable=True))
    # уже существующим организациям даём свежие 14 дней: иначе они сразу попали бы в блокировку
    op.execute("UPDATE organizations SET trial_ends_at = (now() AT TIME ZONE 'utc') + interval '14 days'")
    op.alter_column("organizations", "trial_ends_at", nullable=False)

    op.create_table(
        "payments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=True),
        sa.Column("organization_name", sa.String(length=150), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("method", sa.String(length=20), nullable=False),
        sa.Column("plan", sa.String(length=20), nullable=False),
        sa.Column("months", sa.Integer(), nullable=False),
        sa.Column("paid_at", sa.DateTime(), nullable=False),
        sa.Column("period_start", sa.DateTime(), nullable=False),
        sa.Column("period_end", sa.DateTime(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("recorded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payments_id"), "payments", ["id"], unique=False)
    op.create_index(op.f("ix_payments_organization_id"), "payments", ["organization_id"], unique=False)
    op.create_index(op.f("ix_payments_recorded_by"), "payments", ["recorded_by"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_payments_recorded_by"), table_name="payments")
    op.drop_index(op.f("ix_payments_organization_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_column("organizations", "billing_note")
    op.drop_column("organizations", "is_blocked")
    op.drop_column("organizations", "is_free")
    op.drop_column("organizations", "paid_until")
    op.drop_column("organizations", "trial_ends_at")
    op.drop_column("users", "is_platform_admin")
