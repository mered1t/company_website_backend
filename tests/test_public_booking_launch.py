"""Публичная запись перед запуском: горизонт, отгулы и особые часы, дни за период, защита от спама,
повторный клиент и ссылка в письме /booking/<token>."""
import re
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import models
from core.config import settings
from core.rate_limiter import limiter
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE

PHONE = "+380507770000"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
async def shop(db, org_a):
    org = await db.get(models.Organization, org_a.org_id)
    org.booking_horizon_days = 7
    service = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org_a.org_id, full_name="Launch Master")
    db.add_all([service, master])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=master.id, day_of_week=d, start_time="09:00", end_time="19:00")
                for d in range(7)])
    await db.commit()
    today = (await get_org_now(db, org_a.org_id)).date()
    return service, master, today


def _dates_url(org):
    return f"{SLUG_BASE}/{org.slug}/available-dates"


async def _dates(api, org, service, master, **params):
    r = await api.get(_dates_url(org), params={"master_id": master.id, "service_id": service.id, **params})
    return r


def _body(service, master, start, phone=PHONE, **extra):
    return {"client_full_name": "Launch Guest", "client_phone": phone, "master_id": master.id,
            "service_id": service.id, "start_time": start.isoformat(), **extra}


# ------------------------------------------------------------------ 1. горизонт, отгулы, особые часы
async def test_available_dates_never_go_past_the_horizon_or_into_the_past(api, db, org_a, shop):
    service, master, today = shop
    seen = []
    for month_start in (today.replace(day=1), (today.replace(day=1) + timedelta(days=32)).replace(day=1)):
        r = await _dates(api, org_a, service, master, month=month_start.strftime("%Y-%m"))
        assert r.status_code == 200, r.text
        seen += [d for d in r.json()]
    r = await _dates(api, org_a, service, master,
                     **{"from": (today - timedelta(days=3)).isoformat(), "to": (today + timedelta(days=20)).isoformat()})
    seen += r.json()
    assert seen and all(today.isoformat() <= d <= (today + timedelta(days=7)).isoformat() for d in seen)


async def test_slots_and_booking_respect_the_horizon(api, db, org_a, shop):
    service, master, today = shop
    too_far = today + timedelta(days=8)
    r = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-slots",
                      params={"master_id": master.id, "service_id": service.id, "date": too_far.isoformat()})
    assert r.status_code == 400
    start = (await get_org_now(db, org_a.org_id) + timedelta(days=8)).replace(hour=10, minute=0, second=0, microsecond=0)
    assert (await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start))).status_code == 400
    ok = start - timedelta(days=2)
    assert (await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, ok))).status_code == 201


async def test_time_off_and_special_hours_are_respected(api, db, org_a, shop):
    from datetime import datetime
    service, master, today = shop
    day_off, short_day = today + timedelta(days=2), today + timedelta(days=3)
    db.add(models.TimeOff(master_id=master.id, start_date=datetime.combine(day_off, datetime.min.time()),
                          end_date=datetime.combine(day_off, datetime.min.time()), reason="vacation"))
    db.add(models.WorkingHoursException(master_id=master.id, date=datetime.combine(short_day, datetime.min.time()),
                                       start_time="12:00", end_time="13:00"))
    await db.commit()

    r = await _dates(api, org_a, service, master,
                     **{"from": (today + timedelta(days=1)).isoformat(), "to": (today + timedelta(days=4)).isoformat()})
    assert day_off.isoformat() not in r.json() and short_day.isoformat() in r.json()
    slots = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-slots",
                          params={"master_id": master.id, "service_id": service.id, "date": short_day.isoformat()})
    assert [s["start_time"][11:16] for s in slots.json()] == ["12:00"]
    off = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-slots",
                        params={"master_id": master.id, "service_id": service.id, "date": day_off.isoformat()})
    assert off.json() == []


# ------------------------------------------------------------------ 3. дни за период
async def test_dates_for_a_week(api, db, org_a, shop):
    """Неделя может переходить через границу месяца: теперь это один запрос."""
    service, master, today = shop
    r = await _dates(api, org_a, service, master,
                     **{"from": today.isoformat(), "to": (today + timedelta(days=6)).isoformat()})
    assert r.status_code == 200, r.text
    # мастер работает каждый день; сегодняшний день может уже закончиться
    assert r.json()[-6:] == [(today + timedelta(days=i)).isoformat() for i in range(1, 7)]


@pytest.mark.parametrize("params", [
    {"from": "2030-01-10", "to": "2030-01-09"},
    {"from": "2030-01-01", "to": "2030-02-01"},
    {"from": "2030-01-01"},
    {"to": "2030-01-01"},
    {},
    {"month": "2030-01", "from": "2030-01-01", "to": "2030-01-07"},
])
async def test_bad_date_ranges_are_422(api, db, org_a, shop, params):
    service, master, _ = shop
    assert (await _dates(api, org_a, service, master, **params)).status_code == 422


async def test_31_days_is_the_longest_range(api, db, org_a, shop):
    service, master, _ = shop
    r = await _dates(api, org_a, service, master, **{"from": "2030-01-01", "to": "2030-01-31"})
    assert r.status_code == 200


# ------------------------------------------------------------------ 4. спам
async def test_honeypot_blocks_bots_and_creates_nothing(api, db, org_a, shop):
    service, master, today = shop
    start = (await get_org_now(db, org_a.org_id) + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start, website="http://spam"))
    assert r.status_code == 400 and r.json()["detail"] == "Invalid request"
    clients = (await db.execute(select(func.count()).select_from(models.Client).where(
        models.Client.organization_id == org_a.org_id))).scalar()
    assert clients == 0
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start, website=""))
    assert r.status_code == 201, r.text  # пустое поле — обычный человек


async def test_one_phone_cannot_book_more_than_five_times_a_day(api, db, org_a, shop):
    service, master, _ = shop
    now = await get_org_now(db, org_a.org_id)
    for i in range(5):
        start = (now + timedelta(days=2)).replace(hour=9 + i, minute=0, second=0, microsecond=0)
        r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start))
        assert r.status_code == 201, r.text
        row = await db.get(models.Appointment, r.json()["id"])
        row.status = "cancelled"  # отменяем, чтобы не мешал лимит «3 будущие записи»
        await db.commit()
    start = (now + timedelta(days=3)).replace(hour=10, minute=0, second=0, microsecond=0)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start))
    assert r.status_code == 429, r.text
    other = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start, phone="+380507770001"))
    assert other.status_code == 201, other.text


# ------------------------------------------------------------------ 5. повторный клиент
async def test_returning_client_is_found_by_phone(api, db, org_a, shop):
    service, master, _ = shop
    db.add(models.Client(organization_id=org_a.org_id, full_name="Known Client", phone=PHONE))
    await db.commit()
    start = (await get_org_now(db, org_a.org_id) + timedelta(days=2)).replace(hour=11, minute=0, second=0, microsecond=0)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start))
    assert r.status_code == 201, r.text
    clients = (await db.execute(select(models.Client).where(models.Client.organization_id == org_a.org_id))).scalars().all()
    assert [c.full_name for c in clients] == ["Known Client"]


# ------------------------------------------------------------------ 6. ссылка в письме
async def test_email_link_is_frontend_booking_token(api, db, org_a, shop, sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com/")
    service, master, _ = shop
    start = (await get_org_now(db, org_a.org_id) + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_body(service, master, start, client_email="g@example.com"))
    assert r.status_code == 201, r.text
    match = re.search(r'https://app\.example\.com/booking/([A-Za-z0-9_-]+)"', sent_emails[-1]["html"])
    assert match, sent_emails[-1]["html"]
    assert "manage?token" not in sent_emails[-1]["html"]
    info = await api.get(f"{SLUG_BASE}/booking/{match.group(1)}")
    assert info.status_code == 200 and info.json()["appointment_id"] == r.json()["id"]
