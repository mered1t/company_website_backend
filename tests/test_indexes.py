from sqlalchemy import text

from db.database import engine

EXPECTED = {
    "ix_appointments_org_start",
    "ix_appointments_master_start",
    "ix_activity_logs_org_created",
    "ix_activity_logs_user_id",
    "ix_memberships_master_id",
    "ix_invitations_master_id",
    "ix_client_comments_user_id",
}


async def test_expected_indexes_exist(db):
    result = await db.execute(text("select indexname from pg_indexes where schemaname = 'public'"))
    existing = {row[0] for row in result}
    assert EXPECTED <= existing, f"missing: {EXPECTED - existing}"


def test_engine_checks_connections_before_use():
    assert engine.pool._pre_ping is True