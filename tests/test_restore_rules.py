"""Восстановление удалённого: клиент с уже занятым номером и запись с удалённым мастером, услугой или клиентом."""
from datetime import timedelta

import pytest

import models
from core.time_utils import utc_now
from services.common import get_org_now

PHONE = "+380501110000"


async def _client(api, org, name, phone=PHONE):
    r = await api.post(f"{org.base}/clients", headers=org.headers, json={"full_name": name, "phone": phone})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_client_cannot_be_restored_if_the_phone_is_taken(api, db, org_a):
    old = await _client(api, org_a, "Old Owner")
    assert (await api.delete(f"{org_a.base}/clients/{old}", headers=org_a.headers)).status_code == 204
    await _client(api, org_a, "New Owner")

    r = await api.post(f"{org_a.base}/clients/{old}/restore", headers=org_a.headers)
    assert r.status_code == 400, r.text
    assert r.json()["detail"] == "Cannot restore: another client already uses this phone number"
    db.expire_all()
    assert (await db.get(models.Client, old)).deleted_at is not None


async def test_client_with_a_free_phone_is_restored(api, db, org_a):
    old = await _client(api, org_a, "Returning")
    assert (await api.delete(f"{org_a.base}/clients/{old}", headers=org_a.headers)).status_code == 204
    r = await api.post(f"{org_a.base}/clients/{old}/restore", headers=org_a.headers)
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("deleted", ["master", "service", "client"])
async def test_appointment_cannot_come_back_to_a_deleted_master_service_or_client(api, db, org_a, deleted):
    service = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org_a.org_id, full_name="Gone Master")
    client = models.Client(organization_id=org_a.org_id, full_name="Gone Client", phone="+380501110001")
    db.add_all([service, master, client])
    await db.flush()
    start = (await get_org_now(db, org_a.org_id)) - timedelta(days=3)
    appointment = models.Appointment(
        organization_id=org_a.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(hours=1), status="completed", price=1000, currency="EUR",
        deleted_at=utc_now(),
    )
    db.add(appointment)
    target = {"master": master, "service": service, "client": client}[deleted]
    target.deleted_at = utc_now()
    await db.flush()
    appointment_id, target_model, target_id = appointment.id, type(target), target.id
    await db.commit()

    r = await api.post(f"{org_a.base}/appointments/{appointment_id}/restore", headers=org_a.headers)
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith(f"Cannot restore: the {deleted} of this appointment is deleted")
    db.expire_all()
    assert (await db.get(models.Appointment, appointment_id)).deleted_at is not None

    (await db.get(target_model, target_id)).deleted_at = None
    await db.commit()
    r = await api.post(f"{org_a.base}/appointments/{appointment_id}/restore", headers=org_a.headers)
    assert r.status_code == 200, r.text
