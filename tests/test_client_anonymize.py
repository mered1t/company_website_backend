from datetime import datetime

from sqlalchemy import select

import models

PHONE = "+380501234567"


async def _create_client(api, org, phone=PHONE):
    r = await api.post(
        f"{org.base}/clients", headers=org.headers,
        json={"full_name": "Ivan Petrenko", "phone": phone,
              "email": "ivan@example.com", "notes": "allergic to latex"},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _add_appointment(db, org, client_id, start, notes, status="completed"):
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master One")
    db.add_all([service, master])
    await db.flush()
    appt = models.Appointment(
        organization_id=org.org_id, client_id=client_id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start.replace(hour=start.hour + 1),
        status=status, price=1000, currency="EUR", notes=notes,
    )
    db.add(appt)
    await db.commit()
    return appt.id


async def _reload(db, model, obj_id):
    r = await db.execute(select(model).where(model.id == obj_id).execution_options(populate_existing=True))
    return r.scalars().first()


async def test_anonymize_erases_personal_data_but_keeps_history(api, db, org_a):
    client_id = await _create_client(api, org_a)
    r = await api.post(f"{org_a.base}/clients/{client_id}/comments", headers=org_a.headers,
                       json={"content": "prefers evening slots"})
    assert r.status_code == 201, r.text
    appt_id = await _add_appointment(db, org_a, client_id, datetime(2020, 1, 7, 10), "call his wife Anna")
    db.add(models.ActivityLog(
        organization_id=org_a.org_id, user_id=None, action="created",
        entity_type="appointment", entity_id=appt_id,
        details=f"Public booking by Ivan Petrenko ({PHONE})",
    ))
    await db.commit()

    r = await api.post(f"{org_a.base}/clients/{client_id}/anonymize", headers=org_a.headers)
    assert r.status_code == 204, r.text

    client = await _reload(db, models.Client, client_id)
    assert client.full_name == "Deleted client"
    assert client.phone == f"anon-{client_id}"
    assert client.email is None and client.notes is None and client.birth_date is None
    assert client.deleted_at is not None and client.anonymized_at is not None

    comments = (await db.execute(
        select(models.ClientComment).where(models.ClientComment.client_id == client_id))).scalars().all()
    assert comments == []

    appt = await _reload(db, models.Appointment, appt_id)
    assert appt is not None and appt.notes is None and appt.price == 1000

    logs = (await db.execute(select(models.ActivityLog).where(
        models.ActivityLog.organization_id == org_a.org_id))).scalars().all()
    for entry in logs:
        assert "Ivan" not in (entry.details or "")
        assert PHONE not in (entry.details or "")
    assert any(entry.action == "anonymized" for entry in logs)


async def test_anonymize_is_idempotent(api, org_a):
    client_id = await _create_client(api, org_a)
    for _ in range(2):
        r = await api.post(f"{org_a.base}/clients/{client_id}/anonymize", headers=org_a.headers)
        assert r.status_code == 204, r.text


async def test_anonymize_unknown_client_404(api, org_a):
    r = await api.post(f"{org_a.base}/clients/999999/anonymize", headers=org_a.headers)
    assert r.status_code == 404


async def test_anonymize_requires_auth(api, org_a):
    client_id = await _create_client(api, org_a)
    r = await api.post(f"{org_a.base}/clients/{client_id}/anonymize")
    assert r.status_code == 401


async def test_other_organization_cannot_anonymize(api, db, org_a, org_b):
    client_id = await _create_client(api, org_a)
    r = await api.post(f"{org_a.base}/clients/{client_id}/anonymize", headers=org_b.headers)
    assert r.status_code in (403, 404)
    r = await api.post(f"{org_b.base}/clients/{client_id}/anonymize", headers=org_b.headers)
    assert r.status_code == 404
    client = await _reload(db, models.Client, client_id)
    assert client.anonymized_at is None and client.full_name == "Ivan Petrenko"


async def test_cannot_anonymize_with_upcoming_appointment(api, db, org_a):
    client_id = await _create_client(api, org_a)
    await _add_appointment(db, org_a, client_id, datetime(2099, 1, 7, 10), "x", status="scheduled")
    r = await api.post(f"{org_a.base}/clients/{client_id}/anonymize", headers=org_a.headers)
    assert r.status_code == 400
    client = await _reload(db, models.Client, client_id)
    assert client.anonymized_at is None and client.full_name == "Ivan Petrenko"


async def test_anonymized_client_cannot_be_restored(api, org_a):
    client_id = await _create_client(api, org_a)
    assert (await api.post(f"{org_a.base}/clients/{client_id}/anonymize", headers=org_a.headers)).status_code == 204
    r = await api.post(f"{org_a.base}/clients/{client_id}/restore", headers=org_a.headers)
    assert r.status_code == 400


async def test_phone_can_be_reused_after_anonymization(api, org_a):
    client_id = await _create_client(api, org_a)
    assert (await api.post(f"{org_a.base}/clients/{client_id}/anonymize", headers=org_a.headers)).status_code == 204
    r = await api.post(f"{org_a.base}/clients", headers=org_a.headers,
                       json={"full_name": "New Person", "phone": PHONE})
    assert r.status_code == 201, r.text
