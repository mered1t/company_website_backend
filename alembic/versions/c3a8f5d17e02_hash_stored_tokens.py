"""hash stored tokens

Revision ID: c3a8f5d17e02
Revises: b7e41d2c9a10
Create Date: 2026-10-03
"""
from collections.abc import Sequence

from alembic import op

revision: str = "c3a8f5d17e02"
down_revision: str | Sequence[str] | None = "b7e41d2c9a10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["refresh_tokens", "password_reset_tokens", "email_verification_tokens"]


def upgrade() -> None:
    for table in TABLES:
        # token_urlsafe(32) даёт 43 символа, SHA-256 в hex — ровно 64:
        # по длине отличаем уже захешированные строки (миграция безопасна при повторе)
        op.execute(
            f"UPDATE {table} "
            "SET token = encode(sha256(convert_to(token, 'UTF8')), 'hex') "
            "WHERE length(token) <> 64"
        )


def downgrade() -> None:
    # Хеш необратим: после отката старые токены просто перестанут работать
    pass
