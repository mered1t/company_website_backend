"""appointment overlap exclusion

Revision ID: 29a34eed6598
Revises: 99c2a429a8c7
Create Date: 2026-10-01 17:25:44.850371

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '29a34eed6598'
down_revision: Union[str, Sequence[str], None] = '99c2a429a8c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None



def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(
        "ALTER TABLE appointments ADD CONSTRAINT appointments_no_master_overlap "
        "EXCLUDE USING gist (master_id WITH =, tsrange(start_time, end_time) WITH &&) "
        "WHERE (status <> 'cancelled' AND deleted_at IS NULL)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE appointments DROP CONSTRAINT appointments_no_master_overlap")