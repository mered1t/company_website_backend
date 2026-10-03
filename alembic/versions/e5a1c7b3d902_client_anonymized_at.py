"""client anonymized_at

Revision ID: e5a1c7b3d902
Revises: d9f2b6a41c35
"""
from alembic import op
import sqlalchemy as sa

revision = "e5a1c7b3d902"
down_revision = "d9f2b6a41c35"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("anonymized_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("clients", "anonymized_at")
