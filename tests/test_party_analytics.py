"""Аналитика учитывает записи компаний: сколько человек пришло, а не только сколько было записей."""
from datetime import datetime, timedelta

import models

URL = "/analytics"


async def test_people_are_counted(api, db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Tennis", price=4000, duration_minutes=60,
                             max_party_size=4)
    master = models.Master(organization_id=org_a.org_id, full_name="Coach")
    client = models.Client(organization_id=org_a.org_id, full_name="P", phone="+380670000401")
    db.add_all([service, master, client])
    await db.flush()
    base = datetime(2025, 5, 5, 10)
    for i, (size, status) in enumerate(((3, "completed"), (1, "completed"), (4, "cancelled"), (2, "no_show"))):
        start = base + timedelta(days=i)
        db.add(models.Appointment(organization_id=org_a.org_id, client_id=client.id, service_id=service.id,
                                  master_id=master.id, start_time=start, end_time=start + timedelta(hours=1),
                                  status=status, price=4000, currency="EUR", party_size=size))
    await db.commit()
    period = {"date_from": "2025-05-01T00:00:00", "date_to": "2025-05-31T23:59:59"}
    summary = (await api.get(f"{org_a.base}{URL}/appointments-summary", headers=org_a.headers, params=period)).json()
    assert summary["completed"] == 2 and summary["people_served"] == 4
    workload = (await api.get(f"{org_a.base}{URL}/masters-workload", headers=org_a.headers, params=period)).json()
    assert [(w["appointments_count"], w["people"]) for w in workload] == [(2, 4)]
    popular = (await api.get(f"{org_a.base}{URL}/popular-services", headers=org_a.headers)).json()
    assert popular[0]["times_booked"] == 2 and popular[0]["people"] == 4
