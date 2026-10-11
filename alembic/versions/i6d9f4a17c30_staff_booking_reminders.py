"""staff_booking_reminders: напоминания клиентам и по записям, созданным в CRM; appointments.booked_online

Revision ID: i6d9f4a17c30
Revises: h5c8e3f06b29
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "i6d9f4a17c30"
down_revision: Union[str, Sequence[str], None] = "h5c8e3f06b29"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("organizations", sa.Column(
        "staff_booking_reminders", sa.Boolean(), server_default="true", nullable=False,
    ))
    op.add_column("organizations", sa.Column(
        "client_email_language", sa.String(length=5), server_default="en", nullable=False,
    ))
    # у существующих салонов язык писем клиентам берём у владельца
    op.execute("""
        UPDATE organizations AS o
        SET client_email_language = u.language
        FROM memberships AS m
        JOIN users AS u ON u.id = m.user_id
        WHERE m.organization_id = o.id
          AND m.role = 'owner'
          AND u.language IN ('en', 'pl', 'es', 'uk', 'sq')
    """)
    op.add_column("appointments", sa.Column("booked_online", sa.Boolean(), server_default="false", nullable=False))
    # до этой миграции токены (ссылки «отменить/перенести») были только у онлайн-записей
    op.execute("UPDATE appointments SET booked_online = true WHERE id IN (SELECT appointment_id FROM appointment_tokens)")


def downgrade() -> None:
    op.drop_column("appointments", "booked_online")
    op.drop_column("organizations", "client_email_language")
    op.drop_column("organizations", "staff_booking_reminders")
