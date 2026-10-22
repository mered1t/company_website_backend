"""client_statuses: статусы клиентов — справочник салона, статус у клиента ставится вручную

Существующим салонам добавляется набор по умолчанию на языке писем клиентам (client_email_language).

Revision ID: r8c4d1e7a5b9
Revises: q7b3c9d6f4a8
Create Date: 2026-10-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "r8c4d1e7a5b9"
down_revision: Union[str, Sequence[str], None] = "q7b3c9d6f4a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# копия набора из services/client_statuses.py на момент миграции (миграции не импортируют код приложения)
COLORS = ("#4C8DF6", "#2FA36B", "#C99A2E", "#8E8AA8", "#7A6FD6", "#D2555A", "#9AA0A6")
NAMES = {
    "en": ("New", "Regular", "VIP", "One-time", "Sleeping", "Lost", "Buffer"),
    "pl": ("Nowy", "Stały", "VIP", "Jednorazowy", "Uśpiony", "Utracony", "Bufor"),
    "es": ("Nuevo", "Habitual", "VIP", "Ocasional", "Inactivo", "Perdido", "Búfer"),
    "uk": ("Новий", "Постійний", "VIP", "Разовий", "Сплячий", "Втрачений", "Буфер"),
    "sq": ("I ri", "I rregullt", "VIP", "Njëherësh", "Në gjumë", "I humbur", "Bufer"),
}


def upgrade() -> None:
    op.create_table(
        "client_statuses",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=50), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_client_statuses_id"), "client_statuses", ["id"], unique=False)
    op.create_index(op.f("ix_client_statuses_organization_id"), "client_statuses", ["organization_id"], unique=False)
    op.add_column("clients", sa.Column("status_id", sa.Integer(), nullable=True))
    op.create_index(op.f("ix_clients_status_id"), "clients", ["status_id"], unique=False)
    op.create_foreign_key("clients_status_id_fkey", "clients", "client_statuses", ["status_id"], ["id"],
                          ondelete="SET NULL")

    conn = op.get_bind()
    orgs = conn.execute(sa.text("SELECT id, client_email_language FROM organizations")).all()
    rows = []
    for org_id, language in orgs:
        names = NAMES.get(language or "en", NAMES["en"])
        rows += [{"organization_id": org_id, "name": name, "color": color, "position": i}
                 for i, (name, color) in enumerate(zip(names, COLORS))]
    if rows:
        conn.execute(sa.text(
            "INSERT INTO client_statuses (organization_id, name, color, position, created_at) "
            "VALUES (:organization_id, :name, :color, :position, now())"
        ), rows)


def downgrade() -> None:
    op.drop_constraint("clients_status_id_fkey", "clients", type_="foreignkey")
    op.drop_index(op.f("ix_clients_status_id"), table_name="clients")
    op.drop_column("clients", "status_id")
    op.drop_index(op.f("ix_client_statuses_organization_id"), table_name="client_statuses")
    op.drop_index(op.f("ix_client_statuses_id"), table_name="client_statuses")
    op.drop_table("client_statuses")
