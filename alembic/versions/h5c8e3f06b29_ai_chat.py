"""ai_chat: счётчик сообщений ИИ-чата

Revision ID: h5c8e3f06b29
Revises: g4b7d2e95a18
Create Date: 2026-10-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "h5c8e3f06b29"
down_revision: Union[str, Sequence[str], None] = "g4b7d2e95a18"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("organization_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("model", sa.String(length=100), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("tool_calls", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_ai_chat_messages_id"), "ai_chat_messages", ["id"], unique=False)
    op.create_index(op.f("ix_ai_chat_messages_organization_id"), "ai_chat_messages", ["organization_id"], unique=False)
    op.create_index(op.f("ix_ai_chat_messages_user_id"), "ai_chat_messages", ["user_id"], unique=False)
    op.create_index(op.f("ix_ai_chat_messages_created_at"), "ai_chat_messages", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_ai_chat_messages_created_at"), table_name="ai_chat_messages")
    op.drop_index(op.f("ix_ai_chat_messages_user_id"), table_name="ai_chat_messages")
    op.drop_index(op.f("ix_ai_chat_messages_organization_id"), table_name="ai_chat_messages")
    op.drop_index(op.f("ix_ai_chat_messages_id"), table_name="ai_chat_messages")
    op.drop_table("ai_chat_messages")
