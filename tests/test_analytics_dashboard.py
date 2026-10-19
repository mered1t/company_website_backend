"""Экран аналитики одним запросом: то же самое, что отдельные эндпоинты."""
from datetime import datetime, timedelta

import models

AN = "/analytics"
PERIOD = {"date_from": "2025-01-01T00:00:00", "date_to": "2025-01-31T23:59:59"}


async def test_dashboard_matches_separate_endpoints(api, db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org_a.org_id, full_name="Ann")
    client = models.Client(organization_id=org_a.org_id, full_name="C", phone="+380670000801")
    db.add_all([service, master, client])
    await db.flush()
    for i, status in enumerate(("completed", "completed", "cancelled", "no_show")):
        start = datetime(2025, 1, 6 + i, 10)
        db.add(models.Appointment(organization_id=org_a.org_id, client_id=client.id, service_id=service.id,
                                  master_id=master.id, start_time=start, end_time=start + timedelta(hours=1),
                                  status=status, price=1000, currency="EUR"))
    await db.commit()

    r = await api.get(f"{org_a.base}{AN}/dashboard", headers=org_a.headers, params={**PERIOD, "group_by": "week"})
    assert r.status_code == 200, r.text
    board = r.json()

    async def single(path, **extra):
        resp = await api.get(f"{org_a.base}{AN}/{path}", headers=org_a.headers, params={**PERIOD, **extra})
        return resp.json()

    assert board["summary"] == await single("appointments-summary")
    assert board["revenue_trend"] == await single("revenue-trend", group_by="week")
    assert board["status_trend"] == await single("status-trend", group_by="week")
    assert board["clients"] == await single("clients-summary")
    assert board["busiest_hours"] == await single("busiest-hours")
    assert board["top_clients"] == await single("top-clients", limit=5)
    assert board["popular_services"] == await single("popular-services", limit=5)
    assert board["masters"] == await single("masters-workload")
    assert board["summary"]["completed"] == 2


async def test_dashboard_rules(api, org_a):
    long = {"date_from": "2024-01-01T00:00:00", "date_to": "2025-06-01T00:00:00"}
    assert (await api.get(f"{org_a.base}{AN}/dashboard", headers=org_a.headers, params=long)).status_code == 422
    bad = {**PERIOD, "top_limit": 0}
    assert (await api.get(f"{org_a.base}{AN}/dashboard", headers=org_a.headers, params=bad)).status_code == 422
