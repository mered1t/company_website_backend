"""Общее число строк (X-Total-Count) и ограничения размера у списков: журналы, история клиента, комментарии,
отгулы и особые часы мастера."""
from datetime import date, datetime, timedelta

import models
from core.config import settings

TOTAL = "x-total-count"


async def _client(api, org, phone="+380671230000"):
    r = await api.post(f"{org.base}/clients", headers=org.headers, json={"full_name": "Total Client", "phone": phone})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_organization_activity_has_a_total(api, db, org_a):
    for i in range(3):
        await _client(api, org_a, phone=f"+38067123000{i}")
    full = await api.get(f"{org_a.base}/activity", headers=org_a.headers, params={"limit": 100})
    total = int(full.headers[TOTAL])
    assert total == len(full.json()) and total >= 3
    page = await api.get(f"{org_a.base}/activity", headers=org_a.headers, params={"limit": 2, "skip": 1})
    assert len(page.json()) == 2 and int(page.headers[TOTAL]) == total
    assert page.json() == full.json()[1:3]


async def test_client_activity_has_a_total(api, db, org_a):
    client_id = await _client(api, org_a)
    for text in ("one", "two"):
        await api.post(f"{org_a.base}/clients/{client_id}/comments", headers=org_a.headers, json={"content": text})
    await api.patch(f"{org_a.base}/clients/{client_id}", headers=org_a.headers, json={"full_name": "Renamed"})
    r = await api.get(f"{org_a.base}/clients/{client_id}/activity", headers=org_a.headers, params={"limit": 1})
    assert len(r.json()) == 1 and int(r.headers[TOTAL]) >= 2


async def test_client_appointments_are_paged(api, db, org_a):
    client_id = await _client(api, org_a)
    service = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org_a.org_id, full_name="Pager")
    db.add_all([service, master])
    await db.flush()
    start = datetime(2025, 1, 6, 10)
    for i in range(3):
        db.add(models.Appointment(organization_id=org_a.org_id, client_id=client_id, service_id=service.id,
                                  master_id=master.id, start_time=start + timedelta(days=i),
                                  end_time=start + timedelta(days=i, hours=1), status="completed",
                                  price=1000, currency="EUR"))
    await db.commit()
    url = f"{org_a.base}/clients/{client_id}/appointments"
    r = await api.get(url, headers=org_a.headers, params={"limit": 2})
    assert r.status_code == 200 and len(r.json()) == 2 and r.headers[TOTAL] == "3"
    rest = await api.get(url, headers=org_a.headers, params={"limit": 2, "skip": 2})
    assert len(rest.json()) == 1
    assert (await api.get(url, headers=org_a.headers, params={"limit": 501})).status_code == 422


async def test_comments_are_paged(api, db, org_a):
    client_id = await _client(api, org_a)
    for i in range(3):
        await api.post(f"{org_a.base}/clients/{client_id}/comments", headers=org_a.headers, json={"content": f"c{i}"})
    r = await api.get(f"{org_a.base}/clients/{client_id}/comments", headers=org_a.headers, params={"limit": 2})
    assert [c["content"] for c in r.json()] == ["c2", "c1"] and r.headers[TOTAL] == "3"


async def test_time_off_and_schedule_exceptions_are_paged(api, db, org_a):
    master = models.Master(organization_id=org_a.org_id, full_name="Holiday")
    db.add(master)
    await db.flush()
    for i in range(3):
        day = datetime.combine(date(2031, 1, 1) + timedelta(days=10 * i), datetime.min.time())
        db.add(models.TimeOff(master_id=master.id, start_date=day, end_date=day))
        db.add(models.WorkingHoursException(master_id=master.id, date=day + timedelta(days=2),
                                           start_time="10:00", end_time="12:00"))
    await db.commit()
    for path in ("time-off", "schedule-exceptions"):
        r = await api.get(f"{org_a.base}/masters/{master.id}/{path}", headers=org_a.headers, params={"limit": 2})
        assert r.status_code == 200, r.text
        assert len(r.json()) == 2 and r.headers[TOTAL] == "3", path


async def test_browser_can_read_the_total_header(api, db, org_a):
    origin = settings.frontend_url.rstrip("/")
    r = await api.get(f"{org_a.base}/activity", headers={**org_a.headers, "Origin": origin})
    assert "x-total-count" in r.headers.get("access-control-expose-headers", "").lower()
