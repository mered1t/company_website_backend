import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import models
from services.common import get_org_now
from services.email_service import render_email
from services.i18n import SUPPORTED_LANGUAGES
from core.rate_limiter import limiter
from services.booking_tokens import create_token

SLUG_BASE = "/api/v1/public"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _seed(db, org):
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master Ann")
    db.add_all([service, master])
    await db.flush()
    db.add_all([
        models.WorkingHours(master_id=master.id, day_of_week=d, start_time="09:00", end_time="19:00")
        for d in range(7)
    ])
    await db.commit()
    return service, master


async def _day(db, org, days=3, hour=10, minute=0):
    now = await get_org_now(db, org.org_id)
    return (now + timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def _appointment(db, org, service, master, start, status="scheduled"):
    client = models.Client(organization_id=org.org_id, full_name="Bob", phone=f"+38050{abs(hash(str(start))) % 10**7:07d}")
    db.add(client)
    await db.flush()
    appointment = models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(minutes=service.duration_minutes),
        status=status, price=service.price, currency="EUR",
    )
    db.add(appointment)
    await db.flush()
    appointment_id = appointment.id  # после commit/expire_all обращаться к ORM-объекту нельзя
    token = await create_token(db, appointment_id, "en")
    await db.commit()
    return SimpleNamespace(id=appointment_id), token


def _book_body(service, master, start, **extra):
    return {
        "client_full_name": "Bob", "client_phone": "+380501234567",
        "master_id": master.id, "service_id": service.id, "start_time": start.isoformat(), **extra,
    }


# ------------------------------------------------------------------ письмо при записи

async def test_booking_with_email_sends_confirmation_with_manage_link(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    r = await api.post(
        f"{SLUG_BASE}/{org_a.slug}/book",
        json=_book_body(service, master, start, client_email="bob@example.com", language="pl"),
    )
    assert r.status_code == 201, r.text

    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["to"] == "bob@example.com"
    assert mail["subject"] == "Twoja wizyta została potwierdzona"
    token = re.search(r"/booking/manage\?token=([A-Za-z0-9_-]+)", mail["html"]).group(1)

    info = await api.get(f"{SLUG_BASE}/booking/{token}")
    assert info.status_code == 200
    body = info.json()
    assert body["appointment_id"] == r.json()["id"]
    assert body["status"] == "scheduled"
    assert body["service_name"] == "Haircut"
    assert body["master_name"] == "Master Ann"
    assert body["can_modify"] is True
    assert "client" not in str(body).lower()  # персональных данных клиента в ответе нет


async def test_booking_without_email_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_book_body(service, master, start))
    assert r.status_code == 201
    assert sent_emails == []
    assert (await db.execute(select(func.count()).select_from(models.AppointmentToken))).scalar() == 0


async def test_unknown_language_falls_back_to_english(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    r = await api.post(
        f"{SLUG_BASE}/{org_a.slug}/book",
        json=_book_body(service, master, start, client_email="bob@example.com"),
    )
    assert r.status_code == 201
    assert sent_emails[0]["subject"] == "Your appointment is confirmed"


async def test_token_is_stored_only_as_hash(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_book_body(service, master, start, client_email="bob@example.com"))
    token = re.search(r"token=([A-Za-z0-9_-]+)", sent_emails[0]["html"]).group(1)
    stored = (await db.execute(select(models.AppointmentToken.token_hash))).scalars().all()
    assert stored and token not in stored


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_confirmation_email_renders_in_every_language_and_escapes(language):
    html = render_email(
        "booking_confirmation.html", language,
        organization_name="Barber <b>& Co", service_name="Cut", master_name="Ann",
        when="01.01.2030 10:00", manage_url="https://example.com/booking/manage?token=abc",
    )
    assert "https://example.com/booking/manage?token=abc" in html
    assert "Barber &lt;b&gt;&amp; Co" in html
    assert "<b>& Co" not in html


# ------------------------------------------------------------------ просмотр и токен

async def test_unknown_token_is_404(api):
    for path in ("", "/cancel", "/reschedule", "/available-slots?date=2030-01-01"):
        method = api.get if path in ("", "/available-slots?date=2030-01-01") else api.post
        kwargs = {"json": {"start_time": "2030-01-01T10:00:00"}} if path == "/reschedule" else {}
        r = await method(f"{SLUG_BASE}/booking/not-a-real-token{path}", **kwargs)
        assert r.status_code == 404, path


# ------------------------------------------------------------------ отмена

async def test_cancel_by_token(api, db, org_a):
    service, master = await _seed(db, org_a)
    appointment, token = await _appointment(db, org_a, service, master, await _day(db, org_a))

    r = await api.post(f"{SLUG_BASE}/booking/{token}/cancel")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"
    assert r.json()["can_modify"] is False

    db.expire_all()
    assert (await db.get(models.Appointment, appointment.id)).status == "cancelled"

    again = await api.post(f"{SLUG_BASE}/booking/{token}/cancel")  # повторное нажатие
    assert again.status_code == 200


async def test_cancel_frees_the_slot_for_other_clients(api, db, org_a):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    _, token = await _appointment(db, org_a, service, master, start)
    await api.post(f"{SLUG_BASE}/booking/{token}/cancel")

    r = await api.post(
        f"{SLUG_BASE}/{org_a.slug}/book",
        json=_book_body(service, master, start) | {"client_phone": "+380509999999"},
    )
    assert r.status_code == 201


async def test_cancel_too_late_is_rejected(api, db, org_a):
    service, master = await _seed(db, org_a)
    now = await get_org_now(db, org_a.org_id)
    appointment, token = await _appointment(db, org_a, service, master, now + timedelta(hours=1))

    r = await api.post(f"{SLUG_BASE}/booking/{token}/cancel")
    assert r.status_code == 400
    info = await api.get(f"{SLUG_BASE}/booking/{token}")
    assert info.json()["can_modify"] is False
    db.expire_all()
    assert (await db.get(models.Appointment, appointment.id)).status == "scheduled"


async def test_completed_appointment_cannot_be_cancelled(api, db, org_a):
    service, master = await _seed(db, org_a)
    _, token = await _appointment(db, org_a, service, master, await _day(db, org_a), status="completed")
    assert (await api.post(f"{SLUG_BASE}/booking/{token}/cancel")).status_code == 400


# ------------------------------------------------------------------ перенос

async def test_reschedule_moves_the_same_appointment(api, db, org_a):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    appointment, token = await _appointment(db, org_a, service, master, start)

    new_start = start + timedelta(minutes=15)  # пересекается со старым временем: сама запись не мешает
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": new_start.isoformat()})
    assert r.status_code == 200, r.text
    assert r.json()["appointment_id"] == appointment.id
    assert r.json()["start_time"].startswith(new_start.strftime("%Y-%m-%dT%H:%M"))
    assert r.json()["status"] == "scheduled"

    db.expire_all()
    row = await db.get(models.Appointment, appointment.id)
    assert row.start_time == new_start
    assert row.end_time == new_start + timedelta(minutes=60)
    total = (await db.execute(select(func.count()).select_from(models.Appointment))).scalar()
    assert total == 1  # новой записи и «отмены» в статистике не появилось


async def test_reschedule_to_another_day(api, db, org_a):
    service, master = await _seed(db, org_a)
    _, token = await _appointment(db, org_a, service, master, await _day(db, org_a, days=3))
    new_start = await _day(db, org_a, days=5, hour=15)
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": new_start.isoformat()})
    assert r.status_code == 200
    assert r.json()["start_time"].startswith(new_start.strftime("%Y-%m-%dT%H:%M"))


async def test_reschedule_into_busy_slot_is_rejected(api, db, org_a):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    appointment, token = await _appointment(db, org_a, service, master, start)
    busy_start = start + timedelta(hours=3)
    await _appointment(db, org_a, service, master, busy_start)

    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": busy_start.isoformat()})
    assert r.status_code == 400
    db.expire_all()
    assert (await db.get(models.Appointment, appointment.id)).start_time == start


async def test_reschedule_outside_working_hours_is_rejected(api, db, org_a):
    service, master = await _seed(db, org_a)
    _, token = await _appointment(db, org_a, service, master, await _day(db, org_a))
    night = await _day(db, org_a, days=4, hour=3)
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": night.isoformat()})
    assert r.status_code == 400


async def test_reschedule_to_the_past_is_rejected(api, db, org_a):
    service, master = await _seed(db, org_a)
    _, token = await _appointment(db, org_a, service, master, await _day(db, org_a))
    past = await _day(db, org_a, days=-1)
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": past.isoformat()})
    assert r.status_code == 400


async def test_reschedule_beyond_booking_horizon_is_rejected(api, db, org_a):
    service, master = await _seed(db, org_a)
    _, token = await _appointment(db, org_a, service, master, await _day(db, org_a))
    far = await _day(db, org_a, days=400)
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": far.isoformat()})
    assert r.status_code == 400


async def test_cancelled_appointment_cannot_be_rescheduled(api, db, org_a):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    _, token = await _appointment(db, org_a, service, master, start)
    await api.post(f"{SLUG_BASE}/booking/{token}/cancel")
    r = await api.post(
        f"{SLUG_BASE}/booking/{token}/reschedule",
        json={"start_time": (start + timedelta(hours=2)).isoformat()},
    )
    assert r.status_code == 400


async def test_reschedule_too_late_is_rejected(api, db, org_a):
    service, master = await _seed(db, org_a)
    now = await get_org_now(db, org_a.org_id)
    _, token = await _appointment(db, org_a, service, master, now + timedelta(hours=1))
    new_start = await _day(db, org_a, days=4)
    r = await api.post(f"{SLUG_BASE}/booking/{token}/reschedule", json={"start_time": new_start.isoformat()})
    assert r.status_code == 400


# ------------------------------------------------------------------ свободные слоты при переносе

async def test_available_slots_include_own_time_but_not_other_bookings(api, db, org_a):
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    _, token = await _appointment(db, org_a, service, master, start)
    other = start + timedelta(hours=3)
    await _appointment(db, org_a, service, master, other)

    r = await api.get(f"{SLUG_BASE}/booking/{token}/available-slots", params={"date": start.date().isoformat()})
    assert r.status_code == 200
    starts = {s["start_time"][:16] for s in r.json()}
    assert start.strftime("%Y-%m-%dT%H:%M") in starts  # своё время свободно для переноса
    assert (start + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M") in starts
    assert other.strftime("%Y-%m-%dT%H:%M") not in starts  # чужая запись занята
