"""appointment status no_show

Revision ID: b5c8e1a43d76
Revises: a7d3e5f20c94
Create Date: 2026-10-06 21:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b5c8e1a43d76"
down_revision: Union[str, Sequence[str], None] = "a7d3e5f20c94"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# снимаем проверку статуса, как бы она ни называлась в боевой базе
DROP_STATUS_CHECKS = """
DO $$
DECLARE c record;
BEGIN
    FOR c IN
        SELECT conname FROM pg_constraint
        WHERE conrelid = 'appointments'::regclass AND contype = 'c' AND pg_get_constraintdef(oid) ILIKE '%status%'
    LOOP
        EXECUTE format('ALTER TABLE appointments DROP CONSTRAINT %I', c.conname);
    END LOOP;
END $$;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(DROP_STATUS_CHECKS)
    op.create_check_constraint(
        "ck_appointments_status",
        "appointments",
        "status IN ('scheduled', 'completed', 'cancelled', 'no_show')",
    )


def downgrade() -> None:
    """Downgrade schema."""
    # записи со статусом no_show при откате становятся отменёнными (иначе они нарушат старую проверку)
    op.execute("UPDATE appointments SET status = 'cancelled' WHERE status = 'no_show'")
    op.execute(DROP_STATUS_CHECKS)
    op.create_check_constraint(
        "ck_appointments_status",
        "appointments",
        "status IN ('scheduled', 'completed', 'cancelled')",
    )
