import pytest
from sqlalchemy.exc import IntegrityError

import models
from tests.test_tenant_isolation import make_appointment, make_client, make_master, make_service


async def _setup(api, db, org):
    service_id = await make_service(api, org)
    client_id = await make_client(api, org, "+380990000050")
    master_id = await make_master(api, org)
    appt_id = await make_appointment(db, org, client_id, service_id, master_id)
    return client_id, appt_id


async def test_invalid_status_is_rejected_by_api(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    r = await api.patch(f"{org_a.base}/appointments/{appt_id}", headers=org_a.headers, json={"status": "foo"})
    assert r.status_code == 422


async def test_valid_status_is_accepted(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    r = await api.patch(f"{org_a.base}/appointments/{appt_id}", headers=org_a.headers, json={"status": "completed"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"


async def test_status_filter_validates_value(api, db, org_a):
    client_id, _ = await _setup(api, db, org_a)
    url = f"{org_a.base}/clients/{client_id}/appointments"
    assert (await api.get(url, headers=org_a.headers, params={"status_filter": "foo"})).status_code == 422
    r = await api.get(url, headers=org_a.headers, params={"status_filter": "scheduled"})
    assert r.status_code == 200
    assert len(r.json()) == 1


async def test_database_rejects_invalid_status(api, db, org_a):
    client_id, appt_id = await _setup(api, db, org_a)
    appt = await db.get(models.Appointment, appt_id)
    appt.status = "foo"
    with pytest.raises(IntegrityError):
        await db.commit()