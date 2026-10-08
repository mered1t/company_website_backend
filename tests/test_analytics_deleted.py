"""Аналитика не учитывает удалённые записи и не показывает удалённых клиентов."""
from datetime import datetime

import models
from tests.test_tenant_isolation import make_client, make_master, make_service
from core.time_utils import utc_now

RANGE = "date_from=2030-01-01T00:00:00&date_to=2030-01-31T23:59:59"


async def _completed(db, org, client_id, service_id, master_id, hour, price, deleted=False):
    db.add(models.Appointment(
        organization_id=org.org_id, client_id=client_id, service_id=service_id, master_id=master_id,
        start_time=datetime(2030, 1, 7, hour, 0), end_time=datetime(2030, 1, 7, hour + 1, 0),
        status="completed", price=price, currency="EUR",
        deleted_at=utc_now() if deleted else None,
    ))
    await db.commit()


async def test_analytics_ignore_deleted_appointments(api, db, org_a):
    service_id = await make_service(api, org_a)
    client_id = await make_client(api, org_a, "+380990000001")
    master_id = await make_master(api, org_a)
    await _completed(db, org_a, client_id, service_id, master_id, 10, 100)
    await _completed(db, org_a, client_id, service_id, master_id, 12, 200)
    await _completed(db, org_a, client_id, service_id, master_id, 14, 500, deleted=True)

    base = f"{org_a.base}/analytics"

    r = await api.get(f"{base}/revenue?{RANGE}", headers=org_a.headers)
    assert r.json()["total_revenue"] == 300, r.text

    row = (await api.get(f"{base}/top-clients", headers=org_a.headers)).json()[0]
    assert row["total_spent"] == 300 and row["visits_count"] == 2

    row = (await api.get(f"{base}/popular-services", headers=org_a.headers)).json()[0]
    assert row["total_revenue"] == 300 and row["times_booked"] == 2

    rows = (await api.get(f"{base}/masters-workload?{RANGE}", headers=org_a.headers)).json()
    row = next(r for r in rows if r["master_id"] == master_id)
    assert row["total_revenue"] == 300 and row["appointments_count"] == 2


async def test_deleted_clients_are_not_listed_in_client_analytics(api, db, org_a):
    service_id = await make_service(api, org_a)
    master_id = await make_master(api, org_a)
    active = await make_client(api, org_a, "+380990000001")
    gone = await make_client(api, org_a, "+380990000002")
    await _completed(db, org_a, gone, service_id, master_id, 10, 100)

    r = await api.delete(f"{org_a.base}/clients/{gone}", headers=org_a.headers)
    assert r.status_code == 204, r.text

    top_ids = {c["client_id"] for c in (await api.get(f"{org_a.base}/analytics/top-clients",
                                                      headers=org_a.headers)).json()}
    assert gone not in top_ids

    inactive_ids = {c["client_id"] for c in (await api.get(f"{org_a.base}/analytics/inactive-clients",
                                                           headers=org_a.headers)).json()}
    assert active in inactive_ids      # у активного клиента нет визитов
    assert gone not in inactive_ids    # удалённого не показываем