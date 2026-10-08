import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
import resend
from sqlalchemy import select

import models
from common import get_org_now
from email_service import render_email
from i18n import EMAIL_STRINGS, SUPPORTED_LANGUAGES
from rate_limiter import limiter
from services.booking_tokens import create_token

PUBLIC = "/api/v1/public"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _setup(db, org):
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master Ann")
    db.add_all([service, master])
    await db.flush()
    db.add_all([
        models.WorkingHours(master_id=master.id, day_of_week=d, start_time="09:00", end_time="19:00")
        for d in range(7)
    ])
    db.add(models.MasterService(master_id=master.id, service_id=service.id))
    await db.commit()
    return service, master


async def _when(db, org, *, days=3, hour=10):
    now = await get_org_now(db, org.org_id)
    return (now + timedelta(days=days)).replace(hour=hour, minute=0, second=0, microsecond=0)


async def _booked(db, org, service, master, start, *, email="bob@example.com", client_email=None,
                  language="en", with_token=True, status="scheduled", phone="+380501234567"):
    client = models.Client(organization_id=org.org_id, full_name="Bob", phone=phone, email=client_email)
    db.add(client)
    await db.flush()
    appointment = models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(minutes=service.duration_minutes),
        status=status, price=service.price, currency="EUR",
    )
    db.add(appointment)
    await db.flush()
    appointment_id, client_id = appointment.id, client.id
    token = await create_token(db, appointment_id, language, email) if with_token else None
    await db.commit()
    return SimpleNamespace(id=appointment_id, client_id=client_id, token=token)


def _manage_token(mail) -> str:
    return re.search(r"/booking/manage\?token=([A-Za-z0-9_-]+)", mail["html"]).group(1)


def _fmt(dt) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


# ------------------------------------------------------------------ клиент отменяет по ссылке

async def test_client_cancel_sends_email_in_the_booking_language(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start, language="pl")

    r = await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")
    assert r.status_code == 200, r.text

    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["to"] == "bob@example.com"
    assert mail["subject"] == "Twoja wizyta w Shop ownera została odwołana"
    assert "Haircut" in mail["html"] and "Master Ann" in mail["html"] and _fmt(start) in mail["html"]
    assert "przez salon" not in mail["html"]  # это отмена самим клиентом
    assert "token=" not in mail["html"]  # в письме об отмене нет ссылок


async def test_second_cancel_click_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 200
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 200
    assert len(sent_emails) == 1


async def test_too_late_cancel_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    now = await get_org_now(db, org_a.org_id)
    booked = await _booked(db, org_a, service, master, now + timedelta(hours=1))
    r = await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")
    assert r.status_code == 400
    assert sent_emails == []


async def test_cancel_without_any_email_still_works(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a), email=None, client_email=None)
    r = await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")
    assert r.status_code == 200
    assert sent_emails == []


# ------------------------------------------------------------------ клиент переносит по ссылке

async def test_client_reschedule_sends_new_time_and_a_working_link(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    new_start = start + timedelta(hours=2)
    booked = await _booked(db, org_a, service, master, start, language="pl")

    r = await api.post(f"{PUBLIC}/booking/{booked.token}/reschedule", json={"start_time": new_start.isoformat()})
    assert r.status_code == 200, r.text

    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["to"] == "bob@example.com"
    assert mail["subject"] == "Twoja wizyta w Shop ownera została zmieniona"
    assert _fmt(new_start) in mail["html"] and _fmt(start) in mail["html"]
    assert "Salon zmienił" not in mail["html"]

    info = await api.get(f"{PUBLIC}/booking/{_manage_token(mail)}")
    assert info.status_code == 200
    assert info.json()["appointment_id"] == booked.id
    assert info.json()["start_time"].startswith(new_start.strftime("%Y-%m-%dT%H:%M"))


async def test_failed_reschedule_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)
    other = await _booked(db, org_a, service, master, start + timedelta(hours=3), phone="+380509999999")

    past = (await get_org_now(db, org_a.org_id) - timedelta(days=1)).isoformat()
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/reschedule", json={"start_time": past})).status_code == 400
    taken = (start + timedelta(hours=3)).isoformat()
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/reschedule", json={"start_time": taken})).status_code == 400
    assert sent_emails == []
    assert other.id != booked.id


# ------------------------------------------------------------------ сотрудник отменяет и переносит

async def test_staff_cancel_tells_the_client_it_was_the_salon(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)

    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200, r.text

    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["to"] == "bob@example.com"
    assert mail["subject"] == "Your appointment at Shop ownera was cancelled"
    assert "cancelled by the salon" in mail["html"]
    assert _fmt(start) in mail["html"]


async def test_staff_cancel_of_a_past_appointment_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    now = await get_org_now(db, org_a.org_id)
    booked = await _booked(db, org_a, service, master, now - timedelta(hours=5))
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200
    assert sent_emails == []


async def test_staff_reschedule_sends_new_and_old_time_with_a_working_link(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    new_start = start + timedelta(days=1)
    booked = await _booked(db, org_a, service, master, start, language="es")

    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers,
                        json={"start_time": new_start.isoformat()})
    assert r.status_code == 200, r.text

    assert len(sent_emails) == 1
    mail = sent_emails[0]
    assert mail["subject"] == "Tu cita en Shop ownera ha cambiado"
    assert "El salón ha modificado" in mail["html"]
    assert _fmt(new_start) in mail["html"] and _fmt(start) in mail["html"]
    info = await api.get(f"{PUBLIC}/booking/{_manage_token(mail)}")
    assert info.status_code == 200 and info.json()["appointment_id"] == booked.id


async def test_staff_changing_the_master_sends_an_email_without_old_time(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    second = models.Master(organization_id=org_a.org_id, full_name="Master Boris")
    db.add(second)
    await db.flush()
    db.add(models.MasterService(master_id=second.id, service_id=service.id))
    db.add_all([
        models.WorkingHours(master_id=second.id, day_of_week=d, start_time="09:00", end_time="19:00")
        for d in range(7)
    ])
    second_id = second.id
    await db.commit()
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)

    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"master_id": second_id})
    assert r.status_code == 200, r.text

    assert len(sent_emails) == 1
    html = sent_emails[0]["html"]
    assert "Master Boris" in html
    assert "Previous date and time" not in html


@pytest.mark.parametrize("payload", [
    {"notes": "Please come 5 minutes early"},
    {"status": "completed"},
    {},
])
async def test_other_staff_edits_send_nothing(api, db, org_a, sent_emails, payload):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json=payload)
    assert r.status_code == 200, r.text
    assert sent_emails == []


async def test_saving_the_same_time_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers,
                        json={"start_time": start.isoformat()})
    assert r.status_code == 200
    assert sent_emails == []


async def test_staff_reschedule_into_a_taken_slot_sends_nothing(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)
    await _booked(db, org_a, service, master, start + timedelta(hours=3), phone="+380509999999", email=None)
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers,
                        json={"start_time": (start + timedelta(hours=3)).isoformat()})
    assert r.status_code == 400
    assert sent_emails == []
    tokens = (await db.execute(select(models.AppointmentToken))).scalars().all()
    assert len(tokens) == 2  # новых токенов не появилось: только по одному на каждую запись


# ------------------------------------------------------------------ кому и на каком языке

async def test_staff_created_appointment_uses_the_client_email_and_the_owner_language(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    owner_id = (await db.execute(
        select(models.Membership.user_id).where(
            models.Membership.organization_id == org_a.org_id, models.Membership.role == models.MembershipRole.owner,
        ),
    )).scalar_one()
    owner = await db.get(models.User, owner_id)
    owner.language = "pl"
    await db.commit()
    booked = await _booked(db, org_a, service, master, await _when(db, org_a),
                           with_token=False, client_email="walkin@example.com")

    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200, r.text

    assert len(sent_emails) == 1
    assert sent_emails[0]["to"] == "walkin@example.com"
    assert sent_emails[0]["subject"] == "Twoja wizyta w Shop ownera została odwołana"


async def test_booking_email_wins_over_the_client_profile_email(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a),
                           email="booking@example.com", client_email="profile@example.com")
    await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert [m["to"] for m in sent_emails] == ["booking@example.com"]


async def test_no_email_anywhere_means_no_letter(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a),
                           with_token=False, client_email=None)
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200
    assert sent_emails == []


async def test_anonymized_client_gets_no_email(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    client = await db.get(models.Client, booked.client_id)
    client.anonymized_at = client.created_at
    await db.commit()
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200
    assert sent_emails == []


# ------------------------------------------------------------------ надёжность

async def test_failed_send_does_not_break_the_request(api, db, org_a, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("resend is down")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(broken))
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))

    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


# ------------------------------------------------------------------ тексты

@pytest.mark.parametrize("key", ["booking_cancelled", "booking_changed"])
def test_texts_exist_in_every_language_with_the_same_keys(key):
    keys = set(EMAIL_STRINGS[key]["en"])
    for lang in SUPPORTED_LANGUAGES:
        assert set(EMAIL_STRINGS[key][lang]) == keys, f"{key}/{lang}"


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_templates_render_in_every_language_and_escape_user_text(lang):
    common = dict(organization_name="Salon <b>X</b>", service_name="Cut <i>", master_name="Ann", when="01.01.2030 10:00")
    for by_salon in (False, True):
        html = render_email("booking_cancelled.html", lang, by_salon=by_salon, **common)
        assert "01.01.2030 10:00" in html and "<b>X</b>" not in html and "<i>" not in html
        html = render_email("booking_changed.html", lang, by_salon=by_salon, old_when="31.12.2029 09:00",
                            manage_url="https://example.com/booking/manage?token=abc", **common)
        assert "31.12.2029 09:00" in html and "https://example.com/booking/manage?token=abc" in html
        assert "<b>X</b>" not in html
