"""appointment_clients: открытые игры убраны, в запись можно добавить ещё клиентов из базы

* services.open_games, appointments.open_capacity / join_code — больше не нужны;
* appointments.guest_names (имена гостей без карточек) — вместо них клиенты из базы;
* appointment_participants: остаются клиент, его часть цены и отметка напоминания; ссылки «выйти из игры»,
  отдельный email и язык, число друзей и вышедшие участники удаляются; один клиент — один раз в записи.

Revision ID: s9d5e2f8b6c1
Revises: r8c4d1e7a5b9
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "s9d5e2f8b6c1"
down_revision: Union[str, Sequence[str], None] = "r8c4d1e7a5b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("DELETE FROM appointment_participants WHERE left_at IS NOT NULL")
    # на всякий случай: если клиент оказался в записи дважды, оставляем первую строку
    op.execute("""
        DELETE FROM appointment_participants p USING appointment_participants q
        WHERE p.appointment_id = q.appointment_id AND p.client_id = q.client_id AND p.id > q.id
    """)
    op.drop_constraint("ck_participants_party_size", "appointment_participants", type_="check")
    op.drop_constraint("appointment_participants_token_hash_key", "appointment_participants", type_="unique")
    for column in ("party_size", "email", "language", "token_nonce", "token_hash", "left_at"):
        op.drop_column("appointment_participants", column)
    op.create_unique_constraint("uq_participants_appointment_client", "appointment_participants",
                                ["appointment_id", "client_id"])

    op.drop_constraint("appointments_join_code_key", "appointments", type_="unique")
    op.drop_column("appointments", "join_code")
    op.drop_column("appointments", "open_capacity")
    op.drop_column("appointments", "guest_names")
    op.drop_column("services", "open_games")


def downgrade() -> None:
    op.add_column("services", sa.Column("open_games", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("appointments", sa.Column(
        "guest_names", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb"),
    ))
    op.add_column("appointments", sa.Column("open_capacity", sa.Integer(), nullable=True))
    op.add_column("appointments", sa.Column("join_code", sa.String(length=16), nullable=True))
    op.create_unique_constraint("appointments_join_code_key", "appointments", ["join_code"])

    op.drop_constraint("uq_participants_appointment_client", "appointment_participants", type_="unique")
    op.add_column("appointment_participants", sa.Column("party_size", sa.Integer(), server_default="1",
                                                        nullable=False))
    op.add_column("appointment_participants", sa.Column("email", sa.String(length=120), nullable=True))
    op.add_column("appointment_participants", sa.Column("language", sa.String(length=5), server_default="en",
                                                        nullable=False))
    op.add_column("appointment_participants", sa.Column("token_nonce", sa.String(length=32), nullable=True))
    op.add_column("appointment_participants", sa.Column("token_hash", sa.String(length=64), nullable=True))
    op.add_column("appointment_participants", sa.Column("left_at", sa.DateTime(), nullable=True))
    op.execute("""
        UPDATE appointment_participants
        SET token_nonce = substr(md5(random()::text), 1, 32), token_hash = md5(random()::text || id::text) || md5(id::text)
    """)
    op.alter_column("appointment_participants", "token_nonce", nullable=False)
    op.alter_column("appointment_participants", "token_hash", nullable=False)
    op.create_unique_constraint("appointment_participants_token_hash_key", "appointment_participants", ["token_hash"])
    op.create_check_constraint("ck_participants_party_size", "appointment_participants",
                               "party_size BETWEEN 1 AND 50")
