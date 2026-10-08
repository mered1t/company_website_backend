"""Служебное время (токены, приглашения, created_at) считается в UTC на любой машине."""
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

import models
from tests.test_invitations import verify_in_db
from core.time_utils import utc_now


def test_utc_now_is_naive_utc():
    now = utc_now()
    assert now.tzinfo is None
    assert abs((now - datetime.now(UTC).replace(tzinfo=None)).total_seconds()) < 5


async def test_invitation_expiry_is_computed_in_utc(api, db, org_a):
    await verify_in_db(db, "ownera@example.com")
    r = await api.post(f"{org_a.base}/invitations", headers=org_a.headers,
                       json={"email": "newperson@example.com", "role": "admin"})
    assert r.status_code == 201, r.text

    invitation = (await db.execute(select(models.Invitation))).scalars().one()
    expected = utc_now() + timedelta(days=7)
    assert abs((invitation.expires_at - expected).total_seconds()) < 60