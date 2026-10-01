"""appointment status check

Revision ID: 07adc5afe47b
Revises: 416907442e14
"""
from alembic import op
import sqlalchemy as sa

revision = "07adc5afe47b"
down_revision = "416907442e14"
branch_labels = None
depends_on = None

ALLOWED = ("scheduled", "completed", "cancelled")


def upgrade() -> None:
    # 1. Приводим безобидные варианты к нормальному виду
    op.execute("UPDATE appointments SET status = lower(btrim(status)) WHERE status <> lower(btrim(status))")
    op.execute("UPDATE appointments SET status = 'cancelled' WHERE status IN ('canceled', 'cancel')")

    # 2. Если осталось что-то неизвестное, останавливаемся с понятным сообщением
    bind = op.get_bind()
    bad = bind.execute(
        sa.text("SELECT DISTINCT status FROM appointments WHERE status NOT IN :allowed").bindparams(
            sa.bindparam("allowed", expanding=True)
        ),
        {"allowed": list(ALLOWED)},
    ).fetchall()
    if bad:
        raise RuntimeError(f"Unknown appointment statuses in DB: {[r[0] for r in bad]}")

    # 3. Только теперь добавляем правило
    op.create_check_constraint(
        "ck_appointments_status",
        "appointments",
        "status IN ('scheduled', 'completed', 'cancelled')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_appointments_status", "appointments", type_="check")
