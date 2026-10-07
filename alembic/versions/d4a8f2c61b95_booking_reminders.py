"""booking reminders

Revision ID: d4a8f2c61b95
Revises: b5c8e1a43d76
Create Date: 2026-10-07 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4a8f2c61b95"
down_revision: Union[str, Sequence[str], None] = "b5c8e1a43d76"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("appointment_tokens", sa.Column("email", sa.String(length=120), nullable=True))
    op.add_column("appointments", sa.Column("reminded_start_time", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("appointments", "reminded_start_time")
    op.drop_column("appointment_tokens", "email")
