"""group_bookings_switch: тип бизнеса и переключатель групповых записей у организации

Салоны, которые уже пользуются группами (услуга на несколько человек, открытые игры, запись на компанию),
получают переключатель включённым, чтобы у них ничего не сломалось.

Revision ID: o5f1a7b4d2e6
Revises: n4e9f6a3c0d5
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "o5f1a7b4d2e6"
down_revision: Union[str, Sequence[str], None] = "n4e9f6a3c0d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column("business_type", sa.String(length=20), nullable=True))
    op.add_column("organizations", sa.Column("group_bookings", sa.Boolean(), server_default="false", nullable=False))
    op.execute("""
        UPDATE organizations SET group_bookings = true
        WHERE id IN (SELECT organization_id FROM services WHERE max_party_size > 1 OR open_games)
           OR id IN (SELECT organization_id FROM appointments WHERE party_size > 1)
    """)


def downgrade() -> None:
    op.drop_column("organizations", "group_bookings")
    op.drop_column("organizations", "business_type")
