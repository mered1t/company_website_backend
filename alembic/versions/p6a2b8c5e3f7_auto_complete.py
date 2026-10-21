"""auto_complete: автоотметка «Пришёл» (настройка салона и флаг у записи)

Revision ID: p6a2b8c5e3f7
Revises: o5f1a7b4d2e6
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "p6a2b8c5e3f7"
down_revision: Union[str, Sequence[str], None] = "o5f1a7b4d2e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("auto_complete", sa.String(length=20), server_default="end_of_day",
                                             nullable=False))
    op.add_column("appointments", sa.Column("auto_completed", sa.Boolean(), server_default="false", nullable=False))


def downgrade() -> None:
    op.drop_column("appointments", "auto_completed")
    op.drop_column("organizations", "auto_complete")
