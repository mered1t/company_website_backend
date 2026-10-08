import asyncio
import re
from datetime import timedelta

import pytest
import resend
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import models
from services.common import get_org_now
from core.config import settings
from services.email_service import render_email
from services.i18n import EMAIL_STRINGS, SUPPORTED_LANGUAGES
from main import app
from core.rate_limiter import limiter
from services.booking_tokens import hash_token
from services.reminders import reminder_loop, send_due_reminders
from tests.test_booking_manage import SLUG_BASE, _book_body, _day, _seed
from core.time_utils import utc_now


@pytest.fixture
def session_factory(db):
    """Новые сессии на той же тестовой базе (для параллельных проверок и фоновой задачи)."""
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _booked(db, org, service, master, hours_ahead, *, email="bob@example.com", client_email=None,
                  language="en", booked_days_ago=5, status="scheduled"):
    """Запись, сделанная заранее (booked_days_ago дней назад), начало через hours_ahead часов по времени салона."""
    now = await get_org_now(db, org.org_id)
    start = (now + timedelta(hours=hours_ahead)).replace(second=0, microsecond=0)
    client = models.Client(
        organization_id=org.org_id, full_name="Bob", email=client_email,
        phone=f"+38050{abs(hash((hours_ahead, email, client_email, language))) % 10**7:07d}",
    )
    db.add(client)
    await db.flush()
    appointment = models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(minutes=service.duration_minutes),
        status=status, price=service.price, currency="EUR",
    )
    db.add(appointment)
    await db.flush()
    appointment_id = appointment.id
    db.add(models.AppointmentToken(
        appointment_id=appointment_id, token_hash=hash_token(f"booking-token-{appointment_id}"),
        language=language, email=email, created_at=utc_now() - timedelta(days=booked_days_ago),
    ))
    await db.commit()
    return appointment_id, start


async def _marker(db, appointment_id):
    db.expire_all()
    return (await db.get(models.Appointment, appointment_id)).reminded_start_time


async def _setup(db, org):
    return await _seed(db, org)


# ------------------------------------------------------------------ отправка

async def test_reminder_is_sent_with_working_manage_link(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    appointment_id, start = await _booked(db, org_a, service, master, hours_ahead=10)

    assert await send_due_reminders(db) == 1

    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["to"] == "bob@example.com"
    assert mail["subject"] == f"Reminder: your appointment at Shop ownera"
    assert "Haircut" in mail["html"] and "Master Ann" in mail["html"]
    assert start.strftime("%d.%m.%Y %H:%M") in mail["html"]

    token = re.search(r"/booking/manage\?token=([A-Za-z0-9_-]+)", mail["html"]).group(1)
    info = await api.get(f"{SLUG_BASE}/booking/{token}")  # ссылка из напоминания открывает эту же запись
    assert info.status_code == 200
    assert info.json()["appointment_id"] == appointment_id
    assert info.json()["can_modify"] is True
    assert await _marker(db, appointment_id) == start


async def test_reminder_is_not_sent_twice(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1
    assert await send_due_reminders(db) == 0
    assert len(sent_emails) == 1


async def test_reminder_waits_for_the_window(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    appointment_id, start = await _booked(db, org_a, service, master, hours_ahead=30)
    assert await send_due_reminders(db) == 0  # ещё рано

    appointment = await db.get(models.Appointment, appointment_id)  # «прошло» 10 часов: до начала 20 часов
    appointment.start_time = start - timedelta(hours=10)
    appointment.end_time = appointment.start_time + timedelta(hours=1)
    await db.commit()
    assert await send_due_reminders(db) == 1
    assert len(sent_emails) == 1


async def test_reminder_window_follows_the_setting(db, org_a, sent_emails, monkeypatch):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10)
    monkeypatch.setattr(settings, "reminder_hours_before", 6)
    assert await send_due_reminders(db) == 0
    monkeypatch.setattr(settings, "reminder_hours_before", 12)
    assert await send_due_reminders(db) == 1


@pytest.mark.parametrize("status", ["cancelled", "completed", "no_show"])
async def test_only_scheduled_appointments_get_reminders(db, org_a, sent_emails, status):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10, status=status)
    assert await send_due_reminders(db) == 0
    assert sent_emails == []


async def test_no_reminder_for_past_or_deleted_appointments(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=-2)  # уже началась
    deleted_id, _ = await _booked(db, org_a, service, master, hours_ahead=8, email="deleted@example.com")
    appointment = await db.get(models.Appointment, deleted_id)
    appointment.deleted_at = utc_now()
    await db.commit()

    assert await send_due_reminders(db) == 0
    assert sent_emails == []


async def test_booked_inside_the_window_gets_no_extra_reminder(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    # записались сегодня на сегодня: подтверждение только что пришло
    appointment_id, start = await _booked(db, org_a, service, master, hours_ahead=5, booked_days_ago=0)
    assert await send_due_reminders(db) == 0
    assert sent_emails == []
    assert await _marker(db, appointment_id) == start  # закрыто, проверка не вернётся к этой записи


async def test_rescheduled_appointment_is_reminded_again(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    appointment_id, start = await _booked(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1

    appointment = await db.get(models.Appointment, appointment_id)  # перенесли на час позже
    appointment.start_time = start + timedelta(hours=1)
    appointment.end_time = appointment.start_time + timedelta(hours=1)
    await db.commit()

    assert await send_due_reminders(db) == 1
    assert len(sent_emails) == 2
    assert (start + timedelta(hours=1)).strftime("%d.%m.%Y %H:%M") in sent_emails[1]["html"]


# ------------------------------------------------------------------ кому и на каком языке

async def test_language_comes_from_the_booking(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10, language="pl")
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["subject"] == "Przypomnienie o wizycie w Shop ownera"


async def test_booking_email_wins_over_the_client_profile_email(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10, email="new@example.com", client_email="old@example.com")
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["to"] == "new@example.com"


async def test_falls_back_to_client_email(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10, email=None, client_email="client@example.com")
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["to"] == "client@example.com"


async def test_no_email_no_reminder(db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    appointment_id, _ = await _booked(db, org_a, service, master, hours_ahead=10, email=None, client_email=None)
    assert await send_due_reminders(db) == 0
    assert sent_emails == []
    assert await _marker(db, appointment_id) is None


async def test_public_booking_stores_the_email_for_reminders(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _day(db, org_a)
    r = await api.post(
        f"{SLUG_BASE}/{org_a.slug}/book",
        json=_book_body(service, master, start, client_email="bob@example.com", language="es"),
    )
    assert r.status_code == 201, r.text
    token = (await db.execute(select(models.AppointmentToken))).scalars().one()
    assert token.email == "bob@example.com"
    assert token.language == "es"


# ------------------------------------------------------------------ часовой пояс салона

@pytest.mark.parametrize("tz, hours_ahead, expected", [
    ("Europe/Kiev", 22, 1),            # 22 часа по времени салона (по UTC было бы 25)
    ("America/Los_Angeles", 25, 0),    # 25 часов по времени салона (по UTC было бы 18)
    ("America/Los_Angeles", 22, 1),
])
async def test_window_uses_the_salon_timezone(db, org_a, sent_emails, tz, hours_ahead, expected):
    organization = await db.get(models.Organization, org_a.org_id)
    organization.timezone = tz
    await db.commit()
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=hours_ahead)
    assert await send_due_reminders(db) == expected


async def test_unknown_timezone_of_one_salon_does_not_break_others(db, org_a, org_b, sent_emails):
    service_a, master_a = await _setup(db, org_a)
    service_b, master_b = await _setup(db, org_b)
    await _booked(db, org_b, service_b, master_b, hours_ahead=10, email="b@example.com")
    await _booked(db, org_a, service_a, master_a, hours_ahead=10, email="a@example.com")
    broken = await db.get(models.Organization, org_b.org_id)  # у второго салона испортился часовой пояс
    broken.timezone = "Not/AReal_Zone"
    await db.commit()

    assert await send_due_reminders(db) == 1
    assert [m["to"] for m in sent_emails] == ["a@example.com"]


# ------------------------------------------------------------------ сбои и параллельность

async def test_failed_send_is_retried_on_the_next_check(db, org_a, monkeypatch):
    service, master = await _setup(db, org_a)
    appointment_id, _ = await _booked(db, org_a, service, master, hours_ahead=10)

    def broken(*args, **kwargs):
        raise RuntimeError("resend is down")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(broken))
    assert await send_due_reminders(db) == 0
    assert await _marker(db, appointment_id) is None  # отметка снята, попробуем ещё раз
    tokens = (await db.execute(
        select(func.count()).select_from(models.AppointmentToken).where(models.AppointmentToken.appointment_id == appointment_id)
    )).scalar_one()
    assert tokens == 1  # лишняя ссылка не копится

    sent = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda payload, *a, **k: sent.append(payload) or {"id": "x"}))
    assert await send_due_reminders(db) == 1
    assert len(sent) == 1


async def test_two_parallel_checks_send_one_email(db, org_a, sent_emails, session_factory):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10)

    async def run():
        async with session_factory() as session:
            return await send_due_reminders(session)

    results = await asyncio.gather(run(), run(), run())
    assert sum(results) == 1
    assert len(sent_emails) == 1


# ------------------------------------------------------------------ письма и фоновая задача

def test_reminder_email_has_all_languages_and_keys():
    keys = set(EMAIL_STRINGS["booking_reminder"]["en"])
    for lang in SUPPORTED_LANGUAGES:
        assert set(EMAIL_STRINGS["booking_reminder"][lang]) == keys, lang
        html = render_email(
            "booking_reminder.html", lang, organization_name="Salon <b>X</b>", service_name="Cut",
            master_name="Ann", when="01.01.2030 10:00", manage_url="https://example.com/booking/manage?token=abc",
        )
        assert "https://example.com/booking/manage?token=abc" in html
        assert "<b>X</b>" not in html  # пользовательский текст экранируется


async def test_background_loop_sends_and_stops(db, org_a, sent_emails, session_factory):
    service, master = await _setup(db, org_a)
    await _booked(db, org_a, service, master, hours_ahead=10)

    task = asyncio.create_task(reminder_loop(session_factory, interval_seconds=3600, initial_delay=0))
    for _ in range(50):
        if sent_emails:
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(sent_emails) == 1


async def test_background_loop_survives_errors(monkeypatch, session_factory):
    calls = []

    async def broken(db):
        calls.append(1)
        raise RuntimeError("boom")

    monkeypatch.setattr("services.reminders.send_due_reminders", broken)
    task = asyncio.create_task(reminder_loop(session_factory, interval_seconds=0.05, initial_delay=0))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) >= 2  # после ошибки цикл продолжил работать


async def test_app_starts_and_stops_the_loop_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "reminders_in_app", True)
    started = []

    async def fake_loop(session_factory, interval_seconds):
        started.append(interval_seconds)
        await asyncio.sleep(3600)

    monkeypatch.setattr("main.reminder_loop", fake_loop)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0.05)
        assert started == [settings.reminder_check_interval_seconds]
