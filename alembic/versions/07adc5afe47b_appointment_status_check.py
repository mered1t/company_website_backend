"""appointment status check

Revision ID: 07adc5afe47b
Revises: 416907442e14
Create Date: 2026-10-01 22:07:40.656570

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '07adc5afe47b'
down_revision: Union[str, Sequence[str], None] = '416907442e14'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_appointments_status",
        "appointments",
        "status IN ('scheduled', 'completed', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_appointments_status", "appointments", type_="check")
