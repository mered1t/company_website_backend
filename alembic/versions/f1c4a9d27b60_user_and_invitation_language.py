"""user and invitation language

Revision ID: f1c4a9d27b60
Revises: e5a1c7b3d902
"""
from alembic import op
import sqlalchemy as sa

revision = "f1c4a9d27b60"
down_revision = "e5a1c7b3d902"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("language", sa.String(length=5), server_default="en", nullable=False))
    op.add_column("invitations", sa.Column("language", sa.String(length=5), server_default="en", nullable=False))


def downgrade() -> None:
    op.drop_column("invitations", "language")
    op.drop_column("users", "language")
