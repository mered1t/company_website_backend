import json
from datetime import datetime, timedelta

import pytest

from services import ai_service, analytics_service as svc
import models


def _period(start, end):
    return svc.Period(start, end)


async def _base(db, org):
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    client = models.Client(organization_id=org.org_id, full_name="Anna Secret", phone="+10000001")
    master_a = models.Master(organization_id=org.org_id, full_name="Master A")
    master_b = models.Master(organization_id=org.org_id, full_name="Master B")
    db.add_all([service, client, master_a, master_b])
    await db.flush()
    return service, client, master_a, master_b


def _appt(org, client, service, master, start, status="completed", hours=1):
    return models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(hours=hours), status=status, price=1000, currency="EUR",
    )


async def test_masters_detail_cancellations_per_master(db, org_a):
    service, client, a, b = await _base(db, org_a)
    base = datetime(2020, 1, 6, 10)
    db.add_all([
        _appt(org_a, client, service, a, base),
        _appt(org_a, client, service, a, base + timedelta(hours=2)),
        _appt(org_a, client, service, b, base, status="cancelled"),
        _appt(org_a, client, service, b, base + timedelta(hours=2), status="completed"),
        _appt(org_a, client, service, b, base + timedelta(hours=4), status="cancelled"),
    ])
    await db.commit()
    rows = {r["full_name"]: r for r in await svc.masters_detail(db, org_a.org_id, _period(datetime(2020, 1, 6), datetime(2020, 1, 12, 23, 59, 59)))}
    assert rows["Master A"]["completed"] == 2 and rows["Master A"]["cancelled"] == 0
    assert rows["Master A"]["revenue"] == 2000
    assert rows["Master B"]["appointments_total"] == 3
    assert rows["Master B"]["cancellation_rate_percent"] == 66.7


async def test_deleted_master_with_history_is_visible_but_empty_deleted_is_not(db, org_a):
    service, client, a, b = await _base(db, org_a)
    db.add(_appt(org_a, client, service, a, datetime(2020, 1, 6, 10)))
    from core.time_utils import utc_now
    a.deleted_at = utc_now()
    b.deleted_at = utc_now()
    await db.commit()
    rows = await svc.masters_detail(db, org_a.org_id, _period(datetime(2020, 1, 6), datetime(2020, 1, 12, 23, 59, 59)))
    assert [r["full_name"] for r in rows] == ["Master A"]


async def test_revenue_by_weekday_with_average_per_day(db, org_a):
    service, client, a, _ = await _base(db, org_a)
    db.add_all([
        _appt(org_a, client, service, a, datetime(2020, 1, 6, 10)),   # понедельник
        _appt(org_a, client, service, a, datetime(2020, 1, 13, 10)),  # понедельник
        _appt(org_a, client, service, a, datetime(2020, 1, 11, 10)),  # суббота
    ])
    await db.commit()
    # три недели с 6 по 26 января: по 3 понедельника и 3 субботы
    rows = await svc.revenue_by_weekday(db, org_a.org_id, _period(datetime(2020, 1, 6), datetime(2020, 1, 26, 23, 59, 59)))
    assert len(rows) == 7
    monday, saturday = rows[0], rows[5]
    assert monday["revenue"] == 2000 and monday["completed_appointments"] == 2
    assert monday["days_in_period"] == 3
    assert monday["average_revenue_per_day"] == 667  # 2000 / 3
    assert saturday["revenue"] == 1000
    assert rows[1]["revenue"] == 0


async def test_masters_utilization_with_time_off_and_exceptions(db, org_a):
    service, client, a, _ = await _base(db, org_a)
    for weekday in range(5):  # пн-пт 10:00-18:00 = 8 часов
        db.add(models.WorkingHours(master_id=a.id, day_of_week=weekday, start_time="10:00", end_time="18:00"))
    start = datetime(2020, 1, 6, 10)  # понедельник
    db.add_all([
        _appt(org_a, client, service, a, start),
        _appt(org_a, client, service, a, start + timedelta(days=1)),
        _appt(org_a, client, service, a, start + timedelta(days=2)),
        _appt(org_a, client, service, a, start + timedelta(days=3)),
        _appt(org_a, client, service, a, start + timedelta(days=4), status="cancelled"),  # отменённые не считаются
    ])
    await db.commit()
    period = _period(datetime(2020, 1, 6), datetime(2020, 1, 12, 23, 59, 59))

    def mine(rows):
        return [r for r in rows if r["master_id"] == a.id][0]

    result = mine(await svc.masters_utilization(db, org_a.org_id, period))
    assert result["capacity_hours"] == 40.0
    assert result["booked_hours"] == 4.0
    assert result["utilization_percent"] == 10.0

    # отпуск во вторник: рабочих часов на 8 меньше
    db.add(models.TimeOff(master_id=a.id, start_date=datetime(2020, 1, 7), end_date=datetime(2020, 1, 7, 23, 59)))
    await db.commit()
    result = mine(await svc.masters_utilization(db, org_a.org_id, period))
    assert result["capacity_hours"] == 32.0
    assert result["utilization_percent"] == 12.5

    # исключение в среду: вместо 8 часов только 2
    db.add(models.WorkingHoursException(master_id=a.id, date=datetime(2020, 1, 8), start_time="12:00", end_time="14:00"))
    await db.commit()
    result = mine(await svc.masters_utilization(db, org_a.org_id, period))
    assert result["capacity_hours"] == 26.0
    assert result["utilization_percent"] == 15.4


async def test_utilization_is_none_without_working_hours(db, org_a):
    service, client, a, _ = await _base(db, org_a)
    db.add(_appt(org_a, client, service, a, datetime(2020, 1, 6, 10)))
    await db.commit()
    rows = await svc.masters_utilization(db, org_a.org_id, _period(datetime(2020, 1, 6), datetime(2020, 1, 12, 23, 59, 59)))
    assert all(r["utilization_percent"] is None for r in rows)


async def test_utilization_ignores_future_part_of_period(db, org_a):
    await _base(db, org_a)
    await db.commit()
    future = datetime.now() + timedelta(days=30)
    rows = await svc.masters_utilization(db, org_a.org_id, _period(future, future + timedelta(days=7)))
    assert rows == []


async def test_previous_period_is_adjacent_and_same_length():
    current = _period(datetime(2020, 1, 14), datetime(2020, 1, 20, 23, 59, 59))
    prev = svc.previous_period(current)
    assert prev.date_to < current.date_from
    assert current.date_from - prev.date_to == timedelta(microseconds=1)
    assert prev.date_to - prev.date_from == current.date_to - current.date_from


@pytest.fixture
def fake_model(monkeypatch):
    calls = []

    async def fake(data, language):
        calls.append(data)
        return "Fake report", 1, 1

    monkeypatch.setattr(ai_service, "call_model", fake)
    return calls


async def test_ai_payload_has_new_sections_without_personal_data(api, db, org_a, fake_model):
    row = await db.get(models.Organization, org_a.org_id)
    row.plan = "pro"
    service, client, a, b = await _base(db, org_a)
    db.add_all([
        _appt(org_a, client, service, a, datetime(2020, 1, 14, 10)),
        _appt(org_a, client, service, b, datetime(2020, 1, 14, 12), status="cancelled"),
        _appt(org_a, client, service, a, datetime(2020, 1, 7, 10)),  # предыдущий период
    ])
    await db.commit()
    r = await api.post(
        f"{org_a.base}/analytics/ai-report",
        json={"date_from": "2020-01-14", "date_to": "2020-01-20", "language": "en"},
        headers=org_a.headers,
    )
    assert r.status_code == 200
    data = fake_model[0]
    assert len(data["revenue_by_weekday"]) == 7
    assert data["appointments"]["previous_period"]["total"] == 1
    assert data["appointments"]["total_change_percent"] == 100.0
    assert data["clients"]["previous_period"]["new"] == 1
    names = {m["name"]: m for m in data["masters"]}
    assert names["Master B"]["cancelled"] == 1
    assert "utilization_percent" in names["Master A"]
    payload = json.dumps(data, ensure_ascii=False)
    assert "Anna" not in payload and "+1000000" not in payload
