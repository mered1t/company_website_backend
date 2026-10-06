"""appointment tokens

Revision ID: a7d3e5f20c94
Revises: c3f9a1d85e27
Create Date: 2026-10-06 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a7d3e5f20c94"
down_revision: Union[str, Sequence[str], None] = "c3f9a1d85e27"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "appointment_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=5), server_default="en", nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_appointment_tokens_appointment_id"), "appointment_tokens", ["appointment_id"], unique=False)
    op.create_index(op.f("ix_appointment_tokens_id"), "appointment_tokens", ["id"], unique=False)
    op.create_index(op.f("ix_appointment_tokens_token_hash"), "appointment_tokens", ["token_hash"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_appointment_tokens_token_hash"), table_name="appointment_tokens")
    op.drop_index(op.f("ix_appointment_tokens_id"), table_name="appointment_tokens")
    op.drop_index(op.f("ix_appointment_tokens_appointment_id"), table_name="appointment_tokens")
    op.drop_table("appointment_tokens")
