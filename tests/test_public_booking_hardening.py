"""Публичная запись под нагрузкой и с «кривыми» данными: одновременные запросы, сетка времени, часовой пояс."""
import asyncio
from datetime import timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

import models
from core.rate_limiter import limiter
from services.booking import OFF_GRID
from services.booking_tokens import create_token
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE

PHONE = "+380509998877"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _shop(db, org, start="09:00", end="19:00"):
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=30)
    master = models.Master(organization_id=org.org_id, full_name="Master Grid")
    db.add_all([service, master])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=master.id, day_of_week=d, start_time=start, end_time=end)
                for d in range(7)])
    await db.commit()
    return service, master


async def _at(db, org, hour, minute=0, second=0, days=3):
    now = await get_org_now(db, org.org_id)
    return (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=second, microsecond=0)


def _body(service, master, start, phone=PHONE, **extra):
    return {"client_full_name": "Rush", "client_phone": phone, "master_id": master.id,
            "service_id": service.id, "start_time": start if isinstance(start, str) else start.isoformat(), **extra}


async def _book(api, org, body):
    return await api.post(f"{SLUG_BASE}/{org.slug}/book", json=body)


# ------------------------------------------------------------------ одновременные запросы
async def test_simultaneous_bookings_respect_the_three_upcoming_limit(api, db, org_a):
    service, master = await _shop(db, org_a)
    starts = [await _at(db, org_a, 10 + i) for i in range(6)]
    results = await asyncio.gather(*(_book(api, org_a, _body(service, master, s)) for s in starts))
    codes = sorted(r.status_code for r in results)
    assert codes == [201, 201, 201, 400, 400, 400], [r.text for r in results]
    assert all("3 upcoming" in r.json()["detail"] for r in results if r.status_code == 400)
    count = (await db.execute(select(func.count()).select_from(models.Appointment).where(
        models.Appointment.organization_id == org_a.org_id))).scalar()
    assert count == 3


async def test_two_first_bookings_with_one_new_phone_make_one_client(api, db, org_a):
    service, master = await _shop(db, org_a)
    a, b = await asyncio.gather(
        _book(api, org_a, _body(service, master, await _at(db, org_a, 10))),
        _book(api, org_a, _body(service, master, await _at(db, org_a, 12))),
    )
    assert (a.status_code, b.status_code) == (201, 201), (a.text, b.text)
    clients = (await db.execute(select(func.count()).select_from(models.Client).where(
        models.Client.organization_id == org_a.org_id, models.Client.phone == PHONE))).scalar()
    assert clients == 1


async def test_other_phones_are_not_blocked(api, db, org_a):
    service, master = await _shop(db, org_a)
    starts = [await _at(db, org_a, 10 + i) for i in range(4)]
    results = await asyncio.gather(*(
        _book(api, org_a, _body(service, master, start, phone=f"+38050000000{i}")) for i, start in enumerate(starts)
    ))
    assert [r.status_code for r in results] == [201] * 4


# ------------------------------------------------------------------ сетка времени
@pytest.mark.parametrize("minute, second, ok", [(15, 0, True), (45, 0, True), (7, 0, False), (0, 30, False)])
async def test_public_booking_uses_the_15_minute_grid(api, db, org_a, minute, second, ok):
    service, master = await _shop(db, org_a)
    r = await _book(api, org_a, _body(service, master, await _at(db, org_a, 11, minute, second)))
    if ok:
        assert r.status_code == 201, r.text
    else:
        assert r.status_code == 400 and r.json()["detail"] == OFF_GRID


async def test_grid_starts_at_the_beginning_of_the_working_interval(api, db, org_a):
    service, master = await _shop(db, org_a, start="09:10", end="19:00")
    off = await _book(api, org_a, _body(service, master, await _at(db, org_a, 9, 30)))
    assert off.status_code == 400 and off.json()["detail"] == OFF_GRID
    on = await _book(api, org_a, _body(service, master, await _at(db, org_a, 9, 25)))
    assert on.status_code == 201, on.text
    # список свободного времени идёт по той же сетке: 09:10 и 09:40 заняты записью 09:25-09:55, следующий слот 09:55
    slots = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-slots", params={
        "master_id": master.id, "service_id": service.id, "date": (await _at(db, org_a, 9)).date().isoformat()})
    assert slots.status_code == 200, slots.text
    assert slots.json()[0]["start_time"].endswith("09:55:00")


async def test_client_reschedule_uses_the_grid_too(api, db, org_a):
    service, master = await _shop(db, org_a)
    r = await _book(api, org_a, _body(service, master, await _at(db, org_a, 10), client_email="rush@example.com"))
    assert r.status_code == 201, r.text
    token = await create_token(db, r.json()["id"], "en", "rush@example.com")
    await db.commit()
    off = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule",
                         json={"start_time": (await _at(db, org_a, 14, 7)).isoformat()})
    assert off.status_code == 400 and off.json()["detail"] == OFF_GRID
    on = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule",
                        json={"start_time": (await _at(db, org_a, 14, 15)).isoformat()})
    assert on.status_code == 200, on.text


async def test_staff_can_still_book_any_minute(api, db, org_a):
    service, master = await _shop(db, org_a)
    client = models.Client(organization_id=org_a.org_id, full_name="Phone client", phone="+380671234500")
    db.add(client)
    await db.commit()
    r = await api.post(f"{org_a.base}/appointments", headers=org_a.headers, json={
        "client_id": client.id, "service_id": service.id, "master_id": master.id,
        "start_time": (await _at(db, org_a, 10, 7)).isoformat()})
    assert r.status_code == 201, r.text


# ------------------------------------------------------------------ часовой пояс
@pytest.mark.parametrize("as_utc", [True, False])
async def test_time_with_a_timezone_is_converted_to_salon_time(api, db, org_a, as_utc):
    service, master = await _shop(db, org_a)
    local = await _at(db, org_a, 10)  # 10:00 по Киеву (пояс организации в тестах)
    aware = local.replace(tzinfo=ZoneInfo("Europe/Kiev"))
    sent = aware.astimezone(timezone.utc) if as_utc else aware.astimezone(timezone(timedelta(hours=5)))
    r = await _book(api, org_a, _body(service, master, sent.isoformat()))
    assert r.status_code == 201, r.text
    db.expire_all()
    assert (await db.get(models.Appointment, r.json()["id"])).start_time == local


# ------------------------------------------------------------------ календарь свободных дней
@pytest.mark.parametrize("month, code", [("2026-13", 422), ("abc", 422), ("2026-1", 422), ("2030-01", 200)])
async def test_available_dates_validates_the_month(api, db, org_a, month, code):
    service, master = await _shop(db, org_a)
    r = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-dates",
                      params={"master_id": master.id, "service_id": service.id, "month": month})
    assert r.status_code == code, r.text
