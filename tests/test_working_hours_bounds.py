"""Запись должна целиком уместиться в рабочий интервал мастера в тот же день (не переходить через полночь)."""
from datetime import timedelta

import pytest

import models
from core.rate_limiter import limiter
from services.booking_tokens import create_token
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE

OUTSIDE = "Appointment time is outside master's working hours"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _shop(db, org, intervals=(("10:00", "23:00"),), minutes=120):
    service = models.Service(organization_id=org.org_id, name="Long service", price=1000, duration_minutes=minutes)
    master = models.Master(organization_id=org.org_id, full_name="Night Master")
    client = models.Client(organization_id=org.org_id, full_name="Staff Client", phone="+380670000001")
    db.add_all([service, master, client])
    await db.flush()
    db.add_all([
        models.WorkingHours(master_id=master.id, day_of_week=d, start_time=s, end_time=e)
        for d in range(7) for s, e in intervals
    ])
    await db.commit()
    return service, master, client


async def _at(db, org, hour, minute=0, second=0, days=3):
    now = await get_org_now(db, org.org_id)
    return (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=second, microsecond=0)


def _public(service, master, start):
    return {"client_full_name": "Night Owl", "client_phone": "+380501112233",
            "master_id": master.id, "service_id": service.id, "start_time": start.isoformat()}


async def _staff(api, org, service, master, client, start):
    return await api.post(f"{org.base}/appointments", headers=org.headers, json={
        "client_id": client.id, "service_id": service.id, "master_id": master.id, "start_time": start.isoformat(),
    })


# ------------------------------------------------------------------ через полночь
async def test_public_booking_cannot_run_past_midnight(api, db, org_a):
    service, master, _ = await _shop(db, org_a)  # 10:00-23:00, услуга 2 часа
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_public(service, master, await _at(db, org_a, 22, 30)))
    assert r.status_code == 400 and r.json()["detail"] == OUTSIDE


async def test_late_start_outside_hours_is_rejected_even_if_the_end_looks_early(api, db, org_a):
    service, master, _ = await _shop(db, org_a, intervals=(("09:00", "19:00"),), minutes=60)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_public(service, master, await _at(db, org_a, 23, 30)))
    assert r.status_code == 400 and r.json()["detail"] == OUTSIDE


async def test_staff_create_and_edit_cannot_run_past_midnight(api, db, org_a):
    service, master, client = await _shop(db, org_a)
    r = await _staff(api, org_a, service, master, client, await _at(db, org_a, 22, 30))
    assert r.status_code == 400 and r.json()["detail"] == OUTSIDE

    ok = await _staff(api, org_a, service, master, client, await _at(db, org_a, 12))
    assert ok.status_code == 201, ok.text
    r = await api.patch(f"{org_a.base}/appointments/{ok.json()['id']}", headers=org_a.headers,
                        json={"start_time": (await _at(db, org_a, 23)).isoformat()})
    assert r.status_code == 400 and r.json()["detail"] == OUTSIDE


async def test_client_reschedule_cannot_run_past_midnight(api, db, org_a):
    service, master, client = await _shop(db, org_a)
    start = await _at(db, org_a, 12)
    appointment = models.Appointment(
        organization_id=org_a.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(hours=2), status="scheduled", price=1000, currency="EUR",
        booked_online=True,
    )
    db.add(appointment)
    await db.flush()
    token = await create_token(db, appointment.id, "en", "owl@example.com")
    await db.commit()
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule",
                       json={"start_time": (await _at(db, org_a, 22, 30)).isoformat()})
    assert r.status_code == 400 and r.json()["detail"] == OUTSIDE


# ------------------------------------------------------------------ границы интервала
async def test_ending_exactly_at_closing_time_is_fine(api, db, org_a):
    service, master, _ = await _shop(db, org_a)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_public(service, master, await _at(db, org_a, 21)))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("hour, minute, second, ok", [
    (20, 59, 30, True),   # закончится в 22:59:30
    (21, 0, 30, False),   # закончится в 23:00:30, на полминуты позже закрытия
    (9, 59, 59, False),   # начало на секунду раньше открытия
])
async def test_seconds_are_taken_into_account(api, db, org_a, hour, minute, second, ok):
    service, master, client = await _shop(db, org_a)
    r = await _staff(api, org_a, service, master, client, await _at(db, org_a, hour, minute, second))
    assert (r.status_code == 201) is ok, r.text


async def test_booking_must_fit_inside_one_interval(api, db, org_a):
    """Перерыв на обед: 09-12 и 13-19. Запись 11:30-12:30 задевает перерыв."""
    service, master, client = await _shop(db, org_a, intervals=(("09:00", "12:00"), ("13:00", "19:00")), minutes=60)
    r = await _staff(api, org_a, service, master, client, await _at(db, org_a, 11, 30))
    assert r.status_code == 400 and r.json()["detail"] == OUTSIDE
    r = await _staff(api, org_a, service, master, client, await _at(db, org_a, 13))
    assert r.status_code == 201, r.text
