"""Публичная запись не меняет существующую карточку клиента: номер телефона чужого клиента знает кто угодно."""
from datetime import timedelta

import pytest

import models
from core.rate_limiter import limiter
from services.common import get_org_now
from services.reminders import send_due_reminders
from tests.test_booking_manage import SLUG_BASE, _book_body, _day, _seed

PHONE = "+380501234567"  # тот же номер, что в _book_body


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _crm_client_with_visit(db, org, service, master, *, email=None, hours_ahead=10):
    """Клиент, которого салон завёл сам, и его запись из CRM на ближайшие часы."""
    client = models.Client(organization_id=org.org_id, full_name="Ivan Real", phone=PHONE, email=email)
    db.add(client)
    await db.flush()
    start = ((await get_org_now(db, org.org_id)) + timedelta(hours=hours_ahead)).replace(second=0, microsecond=0)
    appointment = models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(hours=1), status="scheduled", price=1000, currency="EUR",
    )
    db.add(appointment)
    await db.commit()
    return client.id, appointment.id


async def _book_as_stranger(api, org, service, master, start, email="attacker@example.com"):
    body = _book_body(service, master, start, client_full_name="Someone Else", client_email=email)
    r = await api.post(f"{SLUG_BASE}/{org.slug}/book", json=body)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_stranger_cannot_attach_an_email_to_a_client_without_one(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    client_id, _ = await _crm_client_with_visit(db, org_a, service, master, email=None)

    await _book_as_stranger(api, org_a, service, master, await _day(db, org_a))
    db.expire_all()
    client = await db.get(models.Client, client_id)
    assert (client.email, client.full_name) == (None, "Ivan Real")

    sent_emails.clear()
    assert await send_due_reminders(db) == 0  # напоминание по записи из CRM чужаку не уходит
    assert sent_emails == []


async def test_stranger_does_not_replace_an_existing_email(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    client_id, appointment_id = await _crm_client_with_visit(db, org_a, service, master, email="ivan@example.com")

    await _book_as_stranger(api, org_a, service, master, await _day(db, org_a))
    db.expire_all()
    assert (await db.get(models.Client, client_id)).email == "ivan@example.com"

    sent_emails.clear()
    assert await send_due_reminders(db) == 1
    assert [m["to"] for m in sent_emails] == ["ivan@example.com"]

    sent_emails.clear()  # салон отменяет визит Ивана: письмо Ивану, а не чужаку
    r = await api.patch(f"{org_a.base}/appointments/{appointment_id}", headers=org_a.headers,
                        json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    assert [m["to"] for m in sent_emails] == ["ivan@example.com"]


async def test_the_strangers_own_booking_still_mails_the_address_from_the_form(api, db, org_a, sent_emails):
    service, master = await _seed(db, org_a)
    await _crm_client_with_visit(db, org_a, service, master, email="ivan@example.com",
                                 hours_ahead=24 * 6)  # не пересекается с записью через 3 дня в любое время суток
    sent_emails.clear()
    appointment_id = await _book_as_stranger(api, org_a, service, master, await _day(db, org_a))
    assert [m["to"] for m in sent_emails] == ["attacker@example.com"]  # подтверждение своей записи

    sent_emails.clear()
    r = await api.patch(f"{org_a.base}/appointments/{appointment_id}", headers=org_a.headers,
                        json={"status": "cancelled"})
    assert r.status_code == 200
    assert [m["to"] for m in sent_emails] == ["attacker@example.com"]


async def test_new_client_from_the_public_form_keeps_the_email(api, db, org_a):
    service, master = await _seed(db, org_a)
    body = _book_body(service, master, await _day(db, org_a), client_email="new@example.com")
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json=body)
    assert r.status_code == 201, r.text
    db.expire_all()
    appointment = await db.get(models.Appointment, r.json()["id"])
    assert (await db.get(models.Client, appointment.client_id)).email == "new@example.com"
