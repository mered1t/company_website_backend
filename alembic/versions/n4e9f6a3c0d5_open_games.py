"""open_games: открытые игры — свободные места в записи занимают другие люди

Revision ID: n4e9f6a3c0d5
Revises: m3d8e5f29b74
Create Date: 2026-10-20
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "n4e9f6a3c0d5"
down_revision: Union[str, Sequence[str], None] = "m3d8e5f29b74"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("services", sa.Column("open_games", sa.Boolean(), server_default="false", nullable=False))
    op.add_column("appointments", sa.Column("open_capacity", sa.Integer(), nullable=True))
    op.add_column("appointments", sa.Column("join_code", sa.String(length=16), nullable=True))
    op.create_unique_constraint("appointments_join_code_key", "appointments", ["join_code"])
    op.create_table(
        "appointment_participants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("appointment_id", sa.Integer(), nullable=False),
        sa.Column("client_id", sa.Integer(), nullable=False),
        sa.Column("party_size", sa.Integer(), nullable=False),
        sa.Column("price", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("language", sa.String(length=5), server_default="en", nullable=False),
        sa.Column("token_nonce", sa.String(length=32), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("reminded_start_time", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("left_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("party_size BETWEEN 1 AND 50", name="ck_participants_party_size"),
        sa.ForeignKeyConstraint(["appointment_id"], ["appointments.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index(op.f("ix_appointment_participants_id"), "appointment_participants", ["id"], unique=False)
    op.create_index(op.f("ix_appointment_participants_organization_id"), "appointment_participants",
                    ["organization_id"], unique=False)
    op.create_index(op.f("ix_appointment_participants_appointment_id"), "appointment_participants",
                    ["appointment_id"], unique=False)
    op.create_index(op.f("ix_appointment_participants_client_id"), "appointment_participants",
                    ["client_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_appointment_participants_client_id"), table_name="appointment_participants")
    op.drop_index(op.f("ix_appointment_participants_appointment_id"), table_name="appointment_participants")
    op.drop_index(op.f("ix_appointment_participants_organization_id"), table_name="appointment_participants")
    op.drop_index(op.f("ix_appointment_participants_id"), table_name="appointment_participants")
    op.drop_table("appointment_participants")
    op.drop_constraint("appointments_join_code_key", "appointments", type_="unique")
    op.drop_column("appointments", "join_code")
    op.drop_column("appointments", "open_capacity")
    op.drop_column("services", "open_games")
