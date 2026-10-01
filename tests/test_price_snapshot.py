from datetime import datetime

import models
from tests.test_tenant_isolation import make_client, make_master, make_service

PERIOD = {"date_from": "2030-01-01T00:00:00", "date_to": "2030-12-31T23:59:59"}


async def _completed_appointment(api, db, org):
    service_id = await make_service(api, org)  # цена услуги 100
    client_id = await make_client(api, org, "+380990000030")
    master_id = await make_master(api, org)
    appt = models.Appointment(
        organization_id=org.org_id, client_id=client_id, service_id=service_id, master_id=master_id,
        start_time=datetime(2030, 1, 7, 10), end_time=datetime(2030, 1, 7, 11),
        status="completed",
        price=100,
        currency="EUR",
    )
    db.add(appt)
    await db.commit()
    await db.refresh(appt)
    return service_id, appt.id


async def test_changing_service_price_does_not_change_history(api, db, org_a):
    service_id, appt_id = await _completed_appointment(api, db, org_a)

    r = await api.patch(f"{org_a.base}/services/{service_id}", headers=org_a.headers, json={"price": 200})
    assert r.status_code == 200, r.text

    r = await api.get(f"{org_a.base}/appointments/{appt_id}", headers=org_a.headers)
    assert r.json()["price"] == 100

    r = await api.get(f"{org_a.base}/analytics/revenue", headers=org_a.headers, params=PERIOD)
    assert r.status_code == 200, r.text
    assert r.json()["total_revenue"] == 100


async def test_other_analytics_use_price_snapshot(api, db, org_a):
    service_id, _ = await _completed_appointment(api, db, org_a)
    await api.patch(f"{org_a.base}/services/{service_id}", headers=org_a.headers, json={"price": 200})

    r = await api.get(f"{org_a.base}/analytics/top-clients", headers=org_a.headers)
    assert r.json()[0]["total_spent"] == 100

    r = await api.get(f"{org_a.base}/analytics/popular-services", headers=org_a.headers)
    assert r.json()[0]["total_revenue"] == 100

    r = await api.get(f"{org_a.base}/analytics/masters-workload", headers=org_a.headers, params=PERIOD)
    assert sum(m["total_revenue"] for m in r.json()) == 100