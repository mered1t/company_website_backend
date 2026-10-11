"""Напоминания клиентам по записям, которые создал сам салон в CRM (настройка staff_booking_reminders)."""
import re
from datetime import timedelta

import pytest
from sqlalchemy import select

import models
from core.config import settings
from core.rate_limiter import limiter
from core.time_utils import utc_now
from services.common import get_org_now
from services.reminders import send_due_reminders
from tests.test_booking_manage import SLUG_BASE, _seed
from tests.test_org_data import _add_member
from tests.test_reminders import _booked, _marker


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _staff_booking(db, org, service, master, hours_ahead, *, client_email="ann@example.com",
                         status="scheduled", client_deleted=False, created_minutes_ago=0):
    """Запись, которую администратор создал в CRM: токена (онлайн-записи) у неё нет."""
    now = await get_org_now(db, org.org_id)
    start = (now + timedelta(hours=hours_ahead)).replace(second=0, microsecond=0)
    client = models.Client(
        organization_id=org.org_id, full_name="Ann Staff", email=client_email,
        phone=f"+38067{abs(hash((hours_ahead, client_email, status, client_deleted))) % 10**7:07d}",
        deleted_at=utc_now() if client_deleted else None,
    )
    db.add(client)
    await db.flush()
    appointment = models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(minutes=service.duration_minutes),
        status=status, price=service.price, currency="EUR",
        created_at=utc_now() - timedelta(minutes=created_minutes_ago),
    )
    db.add(appointment)
    await db.commit()
    return appointment.id, start


async def _set(db, org, **values):
    row = await db.get(models.Organization, org.org_id)
    for name, value in values.items():
        setattr(row, name, value)
    await db.commit()


# ------------------------------------------------------------------ отправка
async def test_staff_booking_gets_a_reminder_with_a_working_link(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    appointment_id, start = await _staff_booking(db, org_a, service, master, hours_ahead=10)

    assert await send_due_reminders(db) == 1
    [mail] = sent_emails
    assert mail["to"] == "ann@example.com"
    assert mail["subject"] == "Reminder: your appointment at Shop ownera"
    assert start.strftime("%d.%m.%Y %H:%M") in mail["html"]
    assert await _marker(db, appointment_id) == start

    token = re.search(r"/booking/manage\?token=([A-Za-z0-9_-]+)", mail["html"]).group(1)
    info = await api.get(f"{SLUG_BASE}/booking/{token}")
    assert info.status_code == 200 and info.json()["appointment_id"] == appointment_id

    # клиент отменил по ссылке: салон видит это в колокольчике, как при онлайн-записи
    assert (await api.post(f"{SLUG_BASE}/booking/{token}/cancel")).status_code == 200
    db.expire_all()
    notes = (await db.execute(
        select(models.Notification).where(models.Notification.appointment_id == appointment_id),
    )).scalars().all()
    assert {n.type for n in notes} == {"booking_cancelled"}


async def test_language_is_the_salons_setting(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _set(db, org_a, client_email_language="uk")
    await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["subject"] == "Нагадування про запис у Shop ownera"


async def test_online_bookings_keep_their_own_language(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _set(db, org_a, client_email_language="uk")
    await _booked(db, org_a, service, master, hours_ahead=10, language="pl")
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["subject"].startswith("Przypomnienie")


async def test_switched_off_setting_stops_only_staff_bookings(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _set(db, org_a, staff_booking_reminders=False)
    staff_id, _ = await _staff_booking(db, org_a, service, master, hours_ahead=10)
    await _booked(db, org_a, service, master, hours_ahead=11, email="online@example.com")
    assert await send_due_reminders(db) == 1
    assert [m["to"] for m in sent_emails] == ["online@example.com"]
    assert await _marker(db, staff_id) is None


async def test_switching_it_back_on_sends_the_reminder(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _set(db, org_a, staff_booking_reminders=False)
    await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 0
    await _set(db, org_a, staff_booking_reminders=True)
    assert await send_due_reminders(db) == 1


@pytest.mark.parametrize("client_email, client_deleted", [(None, False), ("", False), ("ann@example.com", True)])
async def test_no_email_or_anonymized_client_gets_nothing(db, org_a, sent_emails, client_email, client_deleted):
    service, master = await _seed(db, org_a)
    appointment_id, _ = await _staff_booking(db, org_a, service, master, hours_ahead=10,
                                             client_email=client_email, client_deleted=client_deleted)
    assert await send_due_reminders(db) == 0
    assert sent_emails == []
    assert await _marker(db, appointment_id) is None


async def test_staff_booking_inside_the_window_still_gets_one(db, org_a, sent_emails):
    """Онлайн-клиент только что получил подтверждение, а клиенту из CRM никто ничего не присылал."""
    service, master = await _seed(db, org_a)
    await _staff_booking(db, org_a, service, master, hours_ahead=3, created_minutes_ago=1)
    assert await send_due_reminders(db) == 1


async def test_not_twice_but_again_after_reschedule(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    appointment_id, start = await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1
    assert await send_due_reminders(db) == 0
    appointment = await db.get(models.Appointment, appointment_id)
    appointment.start_time = start + timedelta(hours=2)
    appointment.end_time = appointment.start_time + timedelta(hours=1)
    await db.commit()
    assert await send_due_reminders(db) == 1
    assert len(sent_emails) == 2


async def test_waits_for_the_window_and_skips_other_statuses(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _staff_booking(db, org_a, service, master, hours_ahead=30)
    await _staff_booking(db, org_a, service, master, hours_ahead=5, status="cancelled")
    await _staff_booking(db, org_a, service, master, hours_ahead=-2)
    assert await send_due_reminders(db) == 0


async def test_closed_salon_sends_nothing(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _set(db, org_a, paid_until=None, trial_ends_at=utc_now() - timedelta(days=60))
    await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 0


async def test_setting_is_per_salon(db, org_a, org_b, sent_emails):
    service_a, master_a = await _seed(db, org_a)
    service_b, master_b = await _seed(db, org_b)
    await _set(db, org_a, staff_booking_reminders=False)
    await _staff_booking(db, org_a, service_a, master_a, hours_ahead=10, client_email="a@example.com")
    await _staff_booking(db, org_b, service_b, master_b, hours_ahead=10, client_email="b@example.com")
    assert await send_due_reminders(db) == 1
    assert [m["to"] for m in sent_emails] == ["b@example.com"]


async def test_appointment_created_through_the_api_is_reminded(api, db, org_a, sent_emails, monkeypatch):
    """Настоящий путь: администратор создаёт запись через API."""
    service, master = await _seed(db, org_a)
    monkeypatch.setattr(settings, "reminder_hours_before", 72)
    client = models.Client(organization_id=org_a.org_id, full_name="Phone Client", phone="+380991112233",
                           email="phone.client@example.com")
    db.add(client)
    await db.commit()
    start = ((await get_org_now(db, org_a.org_id)) + timedelta(days=1)).replace(hour=11, minute=0, second=0,
                                                                                  microsecond=0)
    r = await api.post(f"{org_a.base}/appointments", headers=org_a.headers, json={
        "client_id": client.id, "service_id": service.id, "master_id": master.id,
        "start_time": start.isoformat(),
    })
    assert r.status_code in (200, 201), r.text
    db.expire_all()
    assert (await db.get(models.Appointment, r.json()["id"])).booked_online is False
    assert await send_due_reminders(db) == 1
    assert sent_emails[-1]["to"] == "phone.client@example.com"


async def test_public_booking_is_marked_as_online(api, db, org_a):
    from tests.test_booking_manage import _book_body, _day
    service, master = await _seed(db, org_a)
    start = await _day(db, org_a)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=_book_body(service, master, start))
    assert r.status_code == 201, r.text
    db.expire_all()
    assert (await db.get(models.Appointment, r.json()["id"])).booked_online is True


# ------------------------------------------------------------------ настройка
async def test_new_salon_uses_the_owners_language(api):
    r = await api.post("/api/v1/users", json={"username": "ukr", "email": "ukr@example.com",
                                              "password": "Passw0rd!", "accept_terms": True, "language": "uk"})
    assert r.status_code in (200, 201), r.text
    token = (await api.post("/api/v1/users/token", data={"username": "ukr@example.com",
                                                          "password": "Passw0rd!"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    created = await api.post("/api/v1/organizations", json={"name": "Kyiv Barbers"}, headers=headers)
    assert created.status_code == 201
    assert created.json()["client_email_language"] == "uk"
    assert created.json()["staff_booking_reminders"] is True
    [listed] = (await api.get("/api/v1/organizations", headers=headers)).json()
    assert (listed["client_email_language"], listed["staff_booking_reminders"]) == ("uk", True)


async def test_admin_can_change_the_settings(api, db, org_a):
    admin_headers, _ = await _add_member(db, org_a, "admin")
    r = await api.patch(org_a.base, json={"staff_booking_reminders": False, "client_email_language": "pl"},
                        headers=admin_headers)
    assert r.status_code == 200, r.text
    assert (r.json()["staff_booking_reminders"], r.json()["client_email_language"]) == (False, "pl")
    [listed] = (await api.get("/api/v1/organizations", headers=org_a.headers)).json()
    assert (listed["staff_booking_reminders"], listed["client_email_language"]) == (False, "pl")
    db.expire_all()
    org = await db.get(models.Organization, org_a.org_id)
    assert (org.staff_booking_reminders, org.client_email_language) == (False, "pl")


async def test_master_cannot_change_the_settings(api, db, org_a):
    master_headers, _ = await _add_member(db, org_a, "master")
    r = await api.patch(org_a.base, json={"staff_booking_reminders": False}, headers=master_headers)
    assert r.status_code == 403


@pytest.mark.parametrize("body", [
    {"staff_booking_reminders": None},
    {"client_email_language": None},
    {"client_email_language": "ua"},
    {"client_email_language": "ru"},
    {"staff_booking_reminders": "maybe"},
])
async def test_bad_settings_are_422(api, db, org_a, body):
    r = await api.patch(org_a.base, json=body, headers=org_a.headers)
    assert r.status_code == 422
    db.expire_all()
    org = await db.get(models.Organization, org_a.org_id)
    assert (org.staff_booking_reminders, org.client_email_language) == (True, "en")


async def test_no_email_bookings_do_not_take_the_batch(db, org_a, sent_emails, monkeypatch):
    """Записи без email вообще не попадают в выборку, иначе они могли бы занять всю пачку и задержать остальных."""
    import services.reminders as reminders
    monkeypatch.setattr(reminders, "BATCH_LIMIT", 1)
    service, master = await _seed(db, org_a)
    await _staff_booking(db, org_a, service, master, hours_ahead=2, client_email=None)
    await _staff_booking(db, org_a, service, master, hours_ahead=3, client_email="")
    await _staff_booking(db, org_a, service, master, hours_ahead=10, client_email="late@example.com")
    assert await send_due_reminders(db) == 1
    assert [m["to"] for m in sent_emails] == ["late@example.com"]


async def test_switched_off_after_the_first_reminder_stops_repeats(db, org_a, sent_emails):
    """После первого напоминания у записи из CRM появляется ссылка; это не делает её «онлайн-записью»."""
    service, master = await _seed(db, org_a)
    appointment_id, start = await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1
    await _set(db, org_a, staff_booking_reminders=False)
    appointment = await db.get(models.Appointment, appointment_id)
    appointment.start_time = start + timedelta(hours=2)
    appointment.end_time = appointment.start_time + timedelta(hours=1)
    await db.commit()
    assert await send_due_reminders(db) == 0
    assert len(sent_emails) == 1


async def test_online_booking_without_its_link_is_skipped_safely(db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    appointment_id, _ = await _booked(db, org_a, service, master, hours_ahead=10, client_email="bob@example.com")
    await db.execute(models.AppointmentToken.__table__.delete().where(
        models.AppointmentToken.appointment_id == appointment_id))
    await db.commit()
    assert await send_due_reminders(db) == 0
    assert sent_emails == []


# ------------------------------------------------------------------ письма об отмене и переносе по записям из CRM
async def test_cancel_and_change_emails_use_the_salon_language_even_after_a_reminder(api, db, org_a, sent_emails):
    """Напоминание создаёт у записи из CRM ссылку; письма об отмене всё равно берут язык и адрес из настроек и карточки."""
    service, master = await _seed(db, org_a)
    await _set(db, org_a, client_email_language="uk")
    appointment_id, start = await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1
    assert sent_emails[-1]["subject"] == "Нагадування про запис у Shop ownera"

    await _set(db, org_a, client_email_language="pl")
    client = await db.get(models.Client, (await db.get(models.Appointment, appointment_id)).client_id)
    client.email = "new.address@example.com"
    await db.commit()

    r = await api.patch(f"{org_a.base}/appointments/{appointment_id}", headers=org_a.headers,
                        json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    assert sent_emails[-1]["to"] == "new.address@example.com"
    assert sent_emails[-1]["subject"] == "Twoja wizyta w Shop ownera została odwołana"


async def test_client_cancel_by_the_reminder_link_uses_the_salon_language(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _set(db, org_a, client_email_language="pl")
    await _staff_booking(db, org_a, service, master, hours_ahead=10)
    assert await send_due_reminders(db) == 1
    token = re.search(r"/booking/manage\?token=([A-Za-z0-9_-]+)", sent_emails[-1]["html"]).group(1)
    assert (await api.post(f"{SLUG_BASE}/booking/{token}/cancel")).status_code == 200
    assert sent_emails[-1]["subject"] == "Twoja wizyta w Shop ownera została odwołana"


async def test_crm_booking_without_email_gets_no_cancel_email(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    appointment_id, _ = await _staff_booking(db, org_a, service, master, hours_ahead=30, client_email=None)
    r = await api.patch(f"{org_a.base}/appointments/{appointment_id}", headers=org_a.headers,
                        json={"status": "cancelled"})
    assert r.status_code == 200
    assert sent_emails == []
