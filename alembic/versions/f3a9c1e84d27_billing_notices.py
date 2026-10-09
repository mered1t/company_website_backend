"""billing notices: отметки отправленных писем о конце подписки

Revision ID: f3a9c1e84d27
Revises: e7b2c9d41f08
Create Date: 2026-10-09
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a9c1e84d27"
down_revision: Union[str, Sequence[str], None] = "e7b2c9d41f08"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("billing_notice_stage", sa.String(length=10), nullable=True))
    op.add_column("organizations", sa.Column("billing_notice_for", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("organizations", "billing_notice_for")
    op.drop_column("organizations", "billing_notice_stage")
