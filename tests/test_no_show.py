from datetime import datetime, timedelta

import ai_service
import analytics_service as svc
import models
from common import get_org_now
from tests.test_tenant_isolation import make_appointment, make_client, make_master, make_service


async def _setup(api, db, org, phone="+380990000060"):
    service_id = await make_service(api, org)
    client_id = await make_client(api, org, phone)
    master_id = await make_master(api, org)
    appt_id = await make_appointment(db, org, client_id, service_id, master_id)
    return client_id, appt_id


async def _move(db, org, appt_id, start_offset, minutes=60):
    """Двигает запись в базе: start_offset считается от «сейчас» по времени салона."""
    now = await get_org_now(db, org.org_id)
    appointment = await db.get(models.Appointment, appt_id)
    appointment.start_time = now + start_offset
    appointment.end_time = appointment.start_time + timedelta(minutes=minutes)
    await db.commit()


def _patch(api, org, appt_id, **body):
    return api.patch(f"{org.base}/appointments/{appt_id}", headers=org.headers, json=body)


async def _status(db, appt_id):
    db.expire_all()
    return (await db.get(models.Appointment, appt_id)).status


# ------------------------------------------------------------------ когда можно ставить «не пришёл»

async def test_no_show_can_be_set_after_the_appointment(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    await _move(db, org_a, appt_id, timedelta(hours=-3))
    r = await _patch(api, org_a, appt_id, status="no_show")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "no_show"
    assert await _status(db, appt_id) == "no_show"


async def test_no_show_can_be_set_during_the_appointment(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    await _move(db, org_a, appt_id, timedelta(minutes=-10), minutes=60)  # приём идёт: началась 10 минут назад
    r = await _patch(api, org_a, appt_id, status="no_show")
    assert r.status_code == 200, r.text


async def test_no_show_cannot_be_set_for_a_future_appointment(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)  # по умолчанию запись в будущем
    r = await _patch(api, org_a, appt_id, status="no_show")
    assert r.status_code == 400
    assert "cancel" in r.json()["detail"]
    assert await _status(db, appt_id) == "scheduled"


async def test_no_show_cannot_be_set_just_before_the_start(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    await _move(db, org_a, appt_id, timedelta(minutes=5))
    assert (await _patch(api, org_a, appt_id, status="no_show")).status_code == 400


async def test_no_show_appointment_cannot_be_moved_to_the_future(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    await _move(db, org_a, appt_id, timedelta(hours=-3))
    assert (await _patch(api, org_a, appt_id, status="no_show")).status_code == 200

    future = (await get_org_now(db, org_a.org_id)) + timedelta(days=2)
    r = await _patch(api, org_a, appt_id, start_time=future.isoformat())
    assert r.status_code == 400
    assert await _status(db, appt_id) == "no_show"


async def test_editing_notes_of_a_no_show_still_works(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    await _move(db, org_a, appt_id, timedelta(hours=-3))
    await _patch(api, org_a, appt_id, status="no_show")
    r = await _patch(api, org_a, appt_id, notes="did not answer the phone")
    assert r.status_code == 200
    assert r.json()["status"] == "no_show"


async def test_no_show_can_be_corrected_back_to_completed(api, db, org_a):
    _, appt_id = await _setup(api, db, org_a)
    await _move(db, org_a, appt_id, timedelta(hours=-3))
    await _patch(api, org_a, appt_id, status="no_show")
    r = await _patch(api, org_a, appt_id, status="completed")
    assert r.status_code == 200
    assert r.json()["status"] == "completed"


# ------------------------------------------------------------------ аналитика

PERIOD = {"date_from": "2020-01-06T00:00:00", "date_to": "2020-01-12T23:59:59"}


async def _seed_analytics(db, org):
    client = models.Client(organization_id=org.org_id, full_name="Anna", phone="+10000001")
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master One")
    db.add_all([client, service, master])
    await db.flush()

    def appt(day, hour, status):
        return models.Appointment(
            organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
            start_time=datetime(2020, 1, day, hour), end_time=datetime(2020, 1, day, hour + 1),
            status=status, price=1000, currency="EUR",
        )

    db.add_all([
        appt(7, 10, "completed"),
        appt(7, 12, "completed"),
        appt(7, 15, "no_show"),
        appt(8, 10, "cancelled"),
        appt(9, 10, "cancelled"),
    ])
    await db.commit()


async def test_summary_counts_no_shows_and_rate(api, db, org_a):
    await _seed_analytics(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/appointments-summary", params=PERIOD, headers=org_a.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 5
    assert body["completed"] == 2
    assert body["cancelled"] == 2
    assert body["no_show"] == 1  # неявка и отмена считаются раздельно
    assert body["no_show_rate_percent"] == 33.3  # 1 / (2 + 1)
    assert body["average_check"] == 1000  # неявка в средний чек не входит


async def test_no_show_is_not_revenue(api, db, org_a):
    await _seed_analytics(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/revenue", params=PERIOD, headers=org_a.headers)
    assert r.json()["total_revenue"] == 2000  # только два completed


async def test_ai_data_contains_no_shows(db, org_a):
    await _seed_analytics(db, org_a)
    period = svc.make_period(datetime(2020, 1, 6), datetime(2020, 1, 12, 23, 59, 59))
    data = await ai_service.collect_report_data(db, org_a.org_id, period)
    assert data["appointments"]["no_show"] == 1
    assert data["appointments"]["no_show_rate_percent"] == 33.3
    assert "no_show" in data["appointments"]["previous_period"]
    master = data["masters"][0]
    assert master["cancelled"] == 2
    assert master["no_show"] == 1
    assert master["no_show_rate_percent"] == 33.3
    assert "no_show" in ai_service.SYSTEM_PROMPT
