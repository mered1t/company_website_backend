"""service_translations: переводы названия и описания услуги для публичной страницы

Revision ID: q7b3c9d6f4a8
Revises: p6a2b8c5e3f7
Create Date: 2026-10-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "q7b3c9d6f4a8"
down_revision: Union[str, Sequence[str], None] = "p6a2b8c5e3f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("services", sa.Column("translations", postgresql.JSONB(astext_type=sa.Text()),
                                        server_default="{}", nullable=False))


def downgrade() -> None:
    op.drop_column("services", "translations")
