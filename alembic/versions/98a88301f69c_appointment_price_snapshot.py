"""appointment price snapshot

Revision ID: 98a88301f69c
Revises: 29a34eed6598
Create Date: 2026-10-01 17:46:05.524880

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


def upgrade() -> None:
    op.add_column("appointments", sa.Column("price", sa.Integer(), nullable=True))
    op.execute(
        "UPDATE appointments SET price = services.price "
        "FROM services WHERE appointments.service_id = services.id"
    )
    op.alter_column("appointments", "price", nullable=False)


def downgrade() -> None:
    op.drop_column("appointments", "price")