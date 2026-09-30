"""fk ondelete rules

Revision ID: 99c2a429a8c7
Revises: 47fb59884ad5
Create Date: 2026-09-30 18:38:29.448838

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '99c2a429a8c7'
down_revision: Union[str, Sequence[str], None] = '47fb59884ad5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (таблица, колонка, на какую таблицу ссылается, правило)
RULES = [
    ("activity_logs", "user_id", "users", "SET NULL"),
    ("client_comments", "user_id", "users", "SET NULL"),
    ("memberships", "master_id", "masters", "SET NULL"),
    ("invitations", "master_id", "masters", "SET NULL"),
    ("client_comments", "client_id", "clients", "CASCADE"),
    ("master_services", "master_id", "masters", "CASCADE"),
    ("master_services", "service_id", "services", "CASCADE"),
    ("working_hours", "master_id", "masters", "CASCADE"),
    ("time_off", "master_id", "masters", "CASCADE"),
    ("working_hours_exceptions", "master_id", "masters", "CASCADE"),
    ("password_reset_tokens", "user_id", "users", "CASCADE"),
    ("refresh_tokens", "user_id", "users", "CASCADE"),
    ("email_verification_tokens", "user_id", "users", "CASCADE"),
]


def _replace_fk(table, column, ref_table, ondelete):
    inspector = sa.inspect(op.get_bind())
    for fk in inspector.get_foreign_keys(table):
        if fk["constrained_columns"] == [column] and fk["referred_table"] == ref_table:
            op.drop_constraint(fk["name"], table, type_="foreignkey")
    op.create_foreign_key(f"{table}_{column}_fkey", table, ref_table, [column], ["id"], ondelete=ondelete)


def upgrade() -> None:
    for table, column, ref_table, ondelete in RULES:
        _replace_fk(table, column, ref_table, ondelete)


def downgrade() -> None:
    for table, column, ref_table, _ in RULES:
        _replace_fk(table, column, ref_table, None)