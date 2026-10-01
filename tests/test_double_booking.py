from datetime import datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

import models
from tests.test_tenant_isolation import make_client, make_master, make_service

MASTER_BUSY = "Master already has an appointment at this time"


def make_appt(org_id, client_id, service_id, master_id, start_h, end_h, **kwargs):
    base = datetime(2030, 1, 7)
    return models.Appointment(
        organization_id=org_id, client_id=client_id, service_id=service_id, master_id=master_id,
        start_time=base + timedelta(hours=start_h), end_time=base + timedelta(hours=end_h), **kwargs,
        price=100,
        currency="EUR",
    )


@pytest.fixture
async def shop(api, org_a):
    return {
        "org_id": org_a.org_id,
        "service": await make_service(api, org_a),
        "client": await make_client(api, org_a, "+380990000020"),
        "master1": await make_master(api, org_a),
        "master2": await make_master(api, org_a),
    }


def appt_for(shop, master_key, start_h, end_h, **kwargs):
    return make_appt(shop["org_id"], shop["client"], shop["service"], shop[master_key], start_h, end_h, **kwargs)


async def test_overlapping_appointments_are_rejected(db, shop):
    db.add(appt_for(shop, "master1", 10, 11))
    await db.commit()
    db.add(appt_for(shop, "master1", 10.5, 11.5))
    with pytest.raises(IntegrityError):
        await db.commit()


async def test_back_to_back_appointments_are_allowed(db, shop):
    db.add(appt_for(shop, "master1", 10, 11))
    db.add(appt_for(shop, "master1", 11, 12))
    await db.commit()


async def test_cancelled_appointment_does_not_block(db, shop):
    db.add(appt_for(shop, "master1", 10, 11, status="cancelled"))
    db.add(appt_for(shop, "master1", 10, 11))
    await db.commit()


async def test_soft_deleted_appointment_does_not_block(db, shop):
    db.add(appt_for(shop, "master1", 10, 11, deleted_at=datetime(2030, 1, 1)))
    db.add(appt_for(shop, "master1", 10, 11))
    await db.commit()


async def test_different_masters_can_overlap(db, shop):
    db.add(appt_for(shop, "master1", 10, 11))
    db.add(appt_for(shop, "master2", 10, 11))
    await db.commit()


async def test_api_returns_400_not_500_on_overlap(api, db, org_a, shop):
    db.add(appt_for(shop, "master1", 10, 11))
    cancelled = appt_for(shop, "master1", 10.5, 11.5, status="cancelled")
    db.add(cancelled)
    await db.commit()
    await db.refresh(cancelled)

    # возвращаем отменённую запись в работу, а время уже занято
    r = await api.patch(f"{org_a.base}/appointments/{cancelled.id}", headers=org_a.headers,
                        json={"status": "scheduled"})
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == MASTER_BUSY