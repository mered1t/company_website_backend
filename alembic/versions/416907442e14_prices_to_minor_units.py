"""prices to minor units

Revision ID: 416907442e14
Revises: 5b48fe6a92dd
Create Date: 2026-10-01 18:24:17.670071

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '416907442e14'
down_revision: Union[str, Sequence[str], None] = '5b48fe6a92dd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    # было: целые единицы (150 = 150 грн), стало: минимальные единицы (15000)
    op.execute("UPDATE services SET price = price * 100")
    op.execute("UPDATE appointments SET price = price * 100")


def downgrade() -> None:
    op.execute("UPDATE services SET price = price / 100")
    op.execute("UPDATE appointments SET price = price / 100")
