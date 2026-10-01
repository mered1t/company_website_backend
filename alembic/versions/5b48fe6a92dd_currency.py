"""currency

Revision ID: 5b48fe6a92dd
Revises: 98a88301f69c
Create Date: 2026-10-01 18:11:33.348213

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b48fe6a92dd'
down_revision: Union[str, Sequence[str], None] = '98a88301f69c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column("currency", sa.String(3), nullable=False, server_default="EUR"),
    )
    op.add_column("appointments", sa.Column("currency", sa.String(3), nullable=True))
    op.execute(
        "UPDATE appointments SET currency = organizations.currency "
        "FROM organizations WHERE appointments.organization_id = organizations.id"
    )
    op.alter_column("appointments", "currency", nullable=False)


def downgrade() -> None:
    op.drop_column("appointments", "currency")
    op.drop_column("organizations", "currency")
