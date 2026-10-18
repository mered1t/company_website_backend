"""party_size: одна запись на компанию из нескольких человек (теннис вдвоём, аренда корта)

Revision ID: k9f2b6c31e52
Revises: j8e1a5b20d41
Create Date: 2026-10-18
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k9f2b6c31e52"
down_revision: Union[str, Sequence[str], None] = "j8e1a5b20d41"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("services", sa.Column("max_party_size", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_services_max_party_size", "services", "max_party_size BETWEEN 1 AND 50")
    op.add_column("appointments", sa.Column("party_size", sa.Integer(), nullable=False, server_default="1"))
    op.create_check_constraint("ck_appointments_party_size", "appointments", "party_size BETWEEN 1 AND 50")


def downgrade() -> None:
    op.drop_constraint("ck_appointments_party_size", "appointments", type_="check")
    op.drop_column("appointments", "party_size")
    op.drop_constraint("ck_services_max_party_size", "services", type_="check")
    op.drop_column("services", "max_party_size")
