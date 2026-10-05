"""organization plan

Revision ID: b8e2d4f61a73
Revises: f1c4a9d27b60
Create Date: 2026-10-05 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b8e2d4f61a73"
down_revision: Union[str, Sequence[str], None] = "f1c4a9d27b60"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "organizations",
        sa.Column("plan", sa.String(length=20), server_default="basic", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("organizations", "plan")
