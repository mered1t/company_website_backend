"""service emoji

Revision ID: 30d495991fd2
Revises: 07adc5afe47b
Create Date: 2026-10-02 18:54:40.277682

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '30d495991fd2'
down_revision: Union[str, Sequence[str], None] = '07adc5afe47b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("services", sa.Column("emoji", sa.String(length=16), nullable=True))


def downgrade() -> None:
    op.drop_column("services", "emoji")