"""add composite and FK indexes

Revision ID: b7e41d2c9a10
Revises: 30d495991fd2
Create Date: 2026-10-03
"""
from collections.abc import Sequence

from alembic import op

revision: str = "b7e41d2c9a10"
down_revision: str | Sequence[str] | None = "30d495991fd2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEXES = [
    ("ix_appointments_org_start", "appointments", ["organization_id", "start_time"]),
    ("ix_appointments_master_start", "appointments", ["master_id", "start_time"]),
    ("ix_activity_logs_org_created", "activity_logs", ["organization_id", "created_at", "id"]),
    ("ix_activity_logs_user_id", "activity_logs", ["user_id"]),
    ("ix_memberships_master_id", "memberships", ["master_id"]),
    ("ix_invitations_master_id", "invitations", ["master_id"]),
    ("ix_client_comments_user_id", "client_comments", ["user_id"]),
]


def upgrade() -> None:
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade() -> None:
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
