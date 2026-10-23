"""status_codes_and_translation_cache: код у стартовых статусов клиентов и общий кэш автопереводов услуг

* client_statuses.code — new, regular, vip, one_time, sleeping, lost, buffer: по нему фронт переводит
  стартовые статусы на язык интерфейса. У уже созданных салонов код ставится статусам, чьё название совпадает
  со стартовым названием (на любом из языков); переименованные и свои статусы остаются без кода;
* translation_cache — готовые переводы текстов услуг, чтобы одинаковый текст не отправлять в ИИ повторно.

Revision ID: t1e6f3a9c7d2
Revises: s9d5e2f8b6c1
Create Date: 2026-10-23
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "t1e6f3a9c7d2"
down_revision: Union[str, Sequence[str], None] = "s9d5e2f8b6c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# копия стартовых названий на момент миграции (services/client_statuses.py может меняться дальше)
CODES = ("new", "regular", "vip", "one_time", "sleeping", "lost", "buffer")
NAMES = (
    ("New", "Regular", "VIP", "One-time", "Sleeping", "Lost", "Buffer"),
    ("Nowy", "Stały", "VIP", "Jednorazowy", "Uśpiony", "Utracony", "Bufor"),
    ("Nuevo", "Habitual", "VIP", "Ocasional", "Inactivo", "Perdido", "Búfer"),
    ("Новий", "Постійний", "VIP", "Разовий", "Сплячий", "Втрачений", "Буфер"),
    ("I ri", "I rregullt", "VIP", "Njëherësh", "Në gjumë", "I humbur", "Bufer"),
)


def upgrade() -> None:
    op.add_column("client_statuses", sa.Column("code", sa.String(length=20), nullable=True))
    statuses = sa.table("client_statuses", sa.column("name", sa.String), sa.column("code", sa.String))
    for i, code in enumerate(CODES):
        names = sorted({language[i] for language in NAMES})
        op.execute(statuses.update().where(statuses.c.name.in_(names)).values(code=code))

    op.create_table(
        "translation_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("language", sa.String(length=5), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_hash", "language", name="uq_translation_cache_source_language"),
    )


def downgrade() -> None:
    op.drop_table("translation_cache")
    op.drop_column("client_statuses", "code")
