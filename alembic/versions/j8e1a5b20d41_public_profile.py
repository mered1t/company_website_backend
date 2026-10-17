"""public_profile: данные салона и мастеров для публичной страницы записи

Revision ID: j8e1a5b20d41
Revises: i6d9f4a17c30
Create Date: 2026-10-17
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "j8e1a5b20d41"
down_revision: Union[str, Sequence[str], None] = "i6d9f4a17c30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("address", sa.String(length=300), nullable=True))
    op.add_column("organizations", sa.Column("phone", sa.String(length=20), nullable=True))
    op.add_column("organizations", sa.Column("logo_url", sa.String(length=500), nullable=True))
    op.add_column("organizations", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("organizations", sa.Column("privacy_policy_url", sa.String(length=500), nullable=True))
    op.add_column("masters", sa.Column("title", sa.String(length=100), nullable=True))
    op.add_column("masters", sa.Column("bio", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("masters", "bio")
    op.drop_column("masters", "title")
    op.drop_column("organizations", "privacy_policy_url")
    op.drop_column("organizations", "description")
    op.drop_column("organizations", "logo_url")
    op.drop_column("organizations", "phone")
    op.drop_column("organizations", "address")
