"""hash invitation tokens

Revision ID: d9f2b6a41c35
Revises: c3a8f5d17e02
Create Date: 2026-10-03
"""
from collections.abc import Sequence

from alembic import op

revision: str = "d9f2b6a41c35"
down_revision: str | Sequence[str] | None = "c3a8f5d17e02"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # исходные токены — 43 символа, SHA-256 в hex — ровно 64 (миграция безопасна при повторе)
    op.execute(
        "UPDATE invitations "
        "SET token = encode(sha256(convert_to(token, 'UTF8')), 'hex') "
        "WHERE length(token) <> 64"
    )


def downgrade() -> None:
    # Хеш необратим: после отката старые ссылки-приглашения перестанут работать
    pass
