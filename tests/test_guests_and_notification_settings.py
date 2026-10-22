"""Число людей в письмах клиенту и выключатели колокольчика."""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from core.rate_limiter import limiter
from services.common import get_org_now
from services.email_service import render_email
from services.i18n import EMAIL_STRINGS, SUPPORTED_LANGUAGES
from services.reminders import send_due_reminders
from tests.test_booking_manage import SLUG_BASE

BOOKING_TEMPLATES = ("booking_confirmation", "booking_reminder", "booking_cancelled", "booking_changed")


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
async def court(db, org_a):
    tennis = models.Service(organization_id=org_a.org_id, name="Tennis", price=4000, duration_minutes=60,
                            max_party_size=4)
    coach = models.Master(organization_id=org_a.org_id, full_name="Coach")
    client = models.Client(organization_id=org_a.org_id, full_name="Player", phone="+380670000301",
                           email="player@example.com")
    db.add_all([tennis, coach, client])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=coach.id, day_of_week=d, start_time="09:00", end_time="13:00")
                for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=3)
    return SimpleNamespace(tennis=tennis.id, coach=coach.id, client=client.id, day=day)


def _at(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


async def _crm(api, org, court, **body):
    return await api.post(f"{org.base}/appointments", headers=org.headers, json={
        "client_id": court.client, "service_id": court.tennis, "master_id": court.coach,
        "start_time": _at(court.day, 10).isoformat(), **body})


async def _public(api, org, court, **body):
    return await api.post(f"{SLUG_BASE}/{org.slug}/book", json={
        "client_full_name": "Online", "client_phone": "+380671230099", "client_email": "online@example.com",
        "service_id": court.tennis, "master_id": court.coach, "start_time": _at(court.day, 10).isoformat(),
        **body})


# ------------------------------------------------------------------ число людей в письмах
def _people(html: str, label: str, count: int) -> bool:
    """Подпись и число рядом (в строке таблицы или «Подпись: N»), без учёта разметки."""
    text = re.sub(r"<[^>]+>", " ", html)
    return re.search(rf"{label}\s*:?\s*{count}\b", text) is not None


def test_every_booking_email_has_the_people_label():
    for key in BOOKING_TEMPLATES:
        for language in SUPPORTED_LANGUAGES:
            assert EMAIL_STRINGS[key][language]["label_people"], (key, language)


@pytest.mark.parametrize("template", BOOKING_TEMPLATES)
def test_people_row_only_for_a_party(template):
    common = dict(organization_name="Club", service_name="Tennis", master_name="Coach", when="01.01.2030 10:00",
                  manage_url="https://example.com/booking/x", by_salon=False, old_when=None)
    two = render_email(f"{template}.html", "uk", party_size=2, **common)
    assert _people(two, "Кількість людей", 2)
    one = render_email(f"{template}.html", "uk", party_size=1, **common)
    assert "Кількість людей" not in one


async def test_reminder_mentions_the_party(api, db, org_a, court, sent_emails):
    # напоминание по записи из CRM (у клиента есть email): компания из 3 человек в ближайшие 24 часа
    soon = (await get_org_now(db, org_a.org_id)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    db.add(models.Appointment(
        organization_id=org_a.org_id, client_id=court.client, service_id=court.tennis, master_id=court.coach,
        start_time=soon, end_time=soon + timedelta(hours=1), price=4000, currency="EUR", party_size=3))
    await db.commit()
    sent_emails.clear()
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["to"] == "player@example.com" and _people(sent_emails[0]["html"], "People", 3)


# ------------------------------------------------------------------ выключатели колокольчика
async def test_settings_default_on_and_can_be_changed(api, org_a):
    url = f"{org_a.base}/notifications/settings"
    r = await api.get(url, headers=org_a.headers)
    assert r.status_code == 200 and r.json() == {
        "booking_created": True, "booking_cancelled": True, "booking_rescheduled": True}
    r = await api.patch(url, headers=org_a.headers, json={"booking_created": False})
    assert r.json() == {"booking_created": False, "booking_cancelled": True, "booking_rescheduled": True}
    assert (await api.patch(url, headers=org_a.headers, json={"booking_cancelled": None})).status_code == 422
    assert (await api.get(url, headers=org_a.headers)).json()["booking_created"] is False


async def test_switched_off_type_is_not_delivered(api, db, org_a, court):
    url = f"{org_a.base}/notifications/settings"
    await api.patch(url, headers=org_a.headers, json={"booking_created": False})
    assert (await _public(api, org_a, court)).status_code == 201
    count = await api.get(f"{org_a.base}/notifications/unread-count", headers=org_a.headers)
    assert count.json()["unread"] == 0
    await api.patch(url, headers=org_a.headers, json={"booking_created": True})
    r = await _public(api, org_a, court, client_phone="+380671230098", start_time=_at(court.day, 12).isoformat())
    assert r.status_code == 201, r.text
    count = await api.get(f"{org_a.base}/notifications/unread-count", headers=org_a.headers)
    assert count.json()["unread"] == 1


async def test_settings_are_per_person(api, db, org_a, org_b):
    await api.patch(f"{org_a.base}/notifications/settings", headers=org_a.headers, json={"booking_created": False})
    other = await api.get(f"{org_b.base}/notifications/settings", headers=org_b.headers)
    assert other.json()["booking_created"] is True
