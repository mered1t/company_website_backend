"""Письма салону: клиент сам отменил или перенёс запись по ссылке из письма."""
import re
from datetime import timedelta

import pytest
import resend

import models
from core.rate_limiter import limiter
from core.time_utils import utc_now
from services.email_service import render_email
from services.i18n import EMAIL_STRINGS, SUPPORTED_LANGUAGES
from tests.test_booking_emails import PUBLIC, _booked, _fmt, _setup, _when
from tests.test_org_data import _add_member

KEYS = ["salon_booking_cancelled", "salon_booking_rescheduled"]
OWNER = "ownera@example.com"
CLIENT = "bob@example.com"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _to(sent, address):
    return [m for m in sent if m["to"] == address]


async def _member(db, org, role, *, master=None, language="en", username=None):
    user = models.User(username=username or f"{role}-{master or 'x'}", email=f"{username or role}-{master or 'x'}@example.com",
                       password_hash="x", language=language)
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole(role),
                             master_id=master))
    await db.commit()
    return user.email


async def _cancel(api, booked):
    r = await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")
    assert r.status_code == 200, r.text


async def _reschedule(api, booked, new_start):
    r = await api.post(f"{PUBLIC}/booking/{booked.token}/reschedule", json={"start_time": new_start.isoformat()})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ отмена

async def test_owner_is_told_about_a_client_cancellation(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)

    await _cancel(api, booked)

    mails = _to(sent_emails, OWNER)
    assert len(mails) == 1
    mail = mails[0]
    assert mail["subject"] == "A client cancelled an appointment"
    for text in ("Bob", "+380501234567", "Haircut", "Master Ann", _fmt(start), "Shop ownera"):
        assert text in mail["html"], text
    assert "Previous date and time" not in mail["html"]
    assert len(_to(sent_emails, CLIENT)) == 1  # клиент получил свой отдельный email, как раньше


async def test_owners_admins_and_the_masters_account_are_told(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    admin = await _member(db, org_a, "admin", username="adm")
    masters_account = await _member(db, org_a, "master", master=master.id, username="ann")
    other_master = models.Master(organization_id=org_a.org_id, full_name="Other")
    db.add(other_master)
    await db.flush()
    await _member(db, org_a, "master", master=other_master.id, username="other")  # чужой мастер письмо не получает
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))

    await _cancel(api, booked)

    recipients = {m["to"] for m in sent_emails} - {CLIENT}
    assert recipients == {OWNER, admin, masters_account}


async def test_an_address_gets_one_letter_even_if_it_differs_only_in_case(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    twin = models.User(username="twin", email="OwnerA@Example.com", password_hash="x")
    db.add(twin)
    await db.flush()
    db.add(models.Membership(user_id=twin.id, organization_id=org_a.org_id, role=models.MembershipRole.admin))
    await db.commit()
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    await _cancel(api, booked)
    assert len([m for m in sent_emails if m["to"].lower() == OWNER]) == 1


async def test_recipients_get_the_letter_in_their_own_language(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    owner = await db.get(models.User, 1)
    owner.language = "uk"
    await db.commit()
    polish_admin = await _member(db, org_a, "admin", username="pl", language="pl")
    booked = await _booked(db, org_a, service, master, await _when(db, org_a), language="es")

    await _cancel(api, booked)

    assert _to(sent_emails, OWNER)[0]["subject"] == "Клієнт скасував запис"
    assert _to(sent_emails, polish_admin)[0]["subject"] == "Klient anulował wizytę"
    assert _to(sent_emails, CLIENT)[0]["subject"] != "Klient anulował wizytę"  # у клиента язык записи


async def test_second_cancel_click_does_not_repeat_the_letter(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    await _cancel(api, booked)
    await _cancel(api, booked)
    assert len(_to(sent_emails, OWNER)) == 1


async def test_too_late_cancel_writes_to_nobody(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    from services.common import get_org_now
    now = await get_org_now(db, org_a.org_id)
    booked = await _booked(db, org_a, service, master, now + timedelta(hours=1))
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 400
    assert sent_emails == []


async def test_cancel_without_client_email_still_tells_the_salon(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a), email=None, client_email=None)
    await _cancel(api, booked)
    assert [m["to"] for m in sent_emails] == [OWNER]


# ------------------------------------------------------------------ перенос

async def test_owner_is_told_about_a_client_reschedule(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    new_start = start + timedelta(hours=2)
    booked = await _booked(db, org_a, service, master, start)

    await _reschedule(api, booked, new_start)

    mails = _to(sent_emails, OWNER)
    assert len(mails) == 1
    mail = mails[0]
    assert mail["subject"] == "A client rescheduled an appointment"
    for text in ("Bob", "+380501234567", "Haircut", "Master Ann", _fmt(new_start), _fmt(start),
                 "New date and time", "Previous date and time"):
        assert text in mail["html"], text
    assert "token=" not in mail["html"]  # ссылки клиента в письме салону нет


async def test_failed_reschedule_writes_to_nobody(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)
    r = await api.post(f"{PUBLIC}/booking/{booked.token}/reschedule",
                       json={"start_time": (utc_now() - timedelta(days=1)).isoformat()})
    assert r.status_code == 400
    assert sent_emails == []


# ------------------------------------------------------------------ когда писать не нужно

async def test_staff_cancel_and_reschedule_do_not_write_to_the_salon(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)

    r = await api.patch(f"{org_a.base}/appointments/{booked.id}",
                        json={"start_time": (start + timedelta(hours=2)).isoformat()}, headers=org_a.headers)
    assert r.status_code == 200, r.text
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", json={"status": "cancelled"}, headers=org_a.headers)
    assert r.status_code == 200, r.text

    assert _to(sent_emails, OWNER) == []  # сотрудник сам знает, что сделал
    assert len(_to(sent_emails, CLIENT)) == 2


async def test_a_closed_organization_is_not_written_to(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    org = await db.get(models.Organization, org_a.org_id)
    org.paid_until = None
    org.trial_ends_at = utc_now() - timedelta(days=30)  # доступ закрыт
    await db.commit()

    await _cancel(api, booked)

    assert _to(sent_emails, OWNER) == []
    assert len(_to(sent_emails, CLIENT)) == 1  # клиенту отмена подтверждается всегда


async def test_other_organizations_are_not_written_to(api, db, org_a, org_b, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    await _cancel(api, booked)
    assert _to(sent_emails, "ownerb@example.com") == []


async def test_failed_salon_send_does_not_break_the_cancel(api, db, org_a, sent_emails, monkeypatch):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))

    def flaky(payload, *args, **kwargs):
        if payload["to"] == OWNER:
            raise RuntimeError("resend is down")
        sent_emails.append(payload)

    monkeypatch.setattr(resend.Emails, "send", staticmethod(flaky))
    await _cancel(api, booked)
    assert [m["to"] for m in sent_emails] == [CLIENT]  # клиентское письмо ушло, запись отменена


async def test_organization_and_client_names_are_escaped(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    client = await db.get(models.Client, booked.client_id)
    client.full_name = "<script>alert(1)</script>"
    await db.commit()
    await _cancel(api, booked)
    html = _to(sent_emails, OWNER)[0]["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html


# ------------------------------------------------------------------ тексты

@pytest.mark.parametrize("key", KEYS)
def test_texts_exist_in_every_language_with_the_same_keys(key):
    reference = set(EMAIL_STRINGS[key]["en"])
    required = {"subject", "heading", "body", "label_client", "label_phone", "label_service", "label_master",
                "label_when", "button", "footer"}
    assert reference >= required
    if key == "salon_booking_rescheduled":
        assert "label_old_when" in reference
    for lang in SUPPORTED_LANGUAGES:
        assert set(EMAIL_STRINGS[key][lang]) == reference, (key, lang)
        assert all(EMAIL_STRINGS[key][lang][k].strip() for k in reference), (key, lang)


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
@pytest.mark.parametrize("key", KEYS)
def test_templates_render_in_every_language(key, lang):
    old_when = "01.02.2030 10:00" if key == "salon_booking_rescheduled" else None
    html = render_email(f"{key}.html", lang, organization_name="Salon X", client_name="Bob", client_phone="+380",
                        service_name="Cut", master_name="Ann", when="02.02.2030 11:00", old_when=old_when,
                        open_url="https://example.com/")
    texts = EMAIL_STRINGS[key][lang]
    assert "Salon X" in html and "Bob" in html and "02.02.2030 11:00" in html
    assert texts["heading"] in html and texts["label_client"] in html and texts["label_when"] in html
    assert ("01.02.2030 10:00" in html) == (old_when is not None)
    assert re.search(r"\{|\}", html) is None  # не осталось неподставленных значений
