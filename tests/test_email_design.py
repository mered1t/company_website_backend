"""Новый дизайн писем: данные записи в письме, ссылки «в календарь» и файл .ics."""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from core.config import settings
from core.rate_limiter import limiter
from services.booking_tokens import create_token
from services.common import get_org_now
from services.email_service import render_email
from services.email_view import booking_links, booking_view, first_name, format_price
from services.i18n import BOOKING_UI, SUPPORTED_LANGUAGES
from tests.test_booking_manage import SLUG_BASE

ORG = SimpleNamespace(name="Studio Nova", slug="nova", timezone="Europe/Kyiv", address="Khreshchatyk 1, Kyiv",
                      phone="+380441234567", logo_url=None)


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


def _view(**extra):
    return booking_view(organization=ORG, appointment_id=42, start_time=datetime(2030, 3, 14, 10, 0),
                        end_time=datetime(2030, 3, 14, 11, 30), service_name="Haircut", master_name="Ann",
                        price=4550, currency="EUR", client_name="Olena Shevchenko", language="uk", **extra)


def test_price_and_name_helpers():
    assert format_price(1500, "EUR") == "€15"
    assert format_price(1550, "USD") == "$15.50"
    assert format_price(150000, "UAH") == "1500 ₴"
    assert first_name("Olena Shevchenko") == "Olena" and first_name(None) is None


def test_view_has_human_date_and_salon_details():
    view = _view()
    assert view["day_number"] == 14 and view["start_hm"] == "10:00" and view["duration_minutes"] == 90
    assert view["time_range"] == "10:00 – 11:30" and view["price_text"] == "€45.50"
    assert view["client_first_name"] == "Olena" and view["organization_initial"] == "S"
    assert view["book_again_url"].endswith("/book/nova") and "maps" in view["maps_url"]


def test_links_need_a_token(monkeypatch):
    assert "google_calendar_url" not in booking_links(_view(), None)
    monkeypatch.setattr(settings, "backend_url", None)
    links = booking_links(_view(), "tok")
    assert "_calendar" not in links and "calendar_url" not in links
    assert "dates=20300314T100000%2F20300314T113000" in links["google_calendar_url"]
    assert "ctz=Europe%2FKyiv" in links["google_calendar_url"]
    monkeypatch.setattr(settings, "backend_url", "https://api.example.com/")
    assert booking_links(_view(), "tok")["calendar_url"] == \
        "https://api.example.com/api/v1/public/booking/tok/calendar.ics"


def test_every_language_has_the_new_texts():
    keys = set(BOOKING_UI["en"])
    for language in SUPPORTED_LANGUAGES:
        assert set(BOOKING_UI[language]) == keys, language


@pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
@pytest.mark.parametrize("template", ["booking_confirmation", "booking_reminder", "booking_changed",
                                      "booking_cancelled"])
def test_designed_templates_render_with_full_data(template, language, monkeypatch):
    monkeypatch.setattr(settings, "backend_url", "https://api.example.com")
    html = render_email(f"{template}.html", language, organization_name=ORG.name, service_name="Haircut",
                        master_name="Ann", when="14.03.2030 10:00", old_when="13.03.2030 09:00",
                        manage_url="https://app.example.com/booking/tok", by_salon=True, party_size=1,
                        **booking_links(_view(), "tok"))
    assert "Studio Nova" in html and "Haircut" in html and not re.search(r"\{[a-z_]+\}", html)
    assert "#0A0B10" in html  # тёмный фон
    if template != "booking_cancelled":
        assert "calendar.ics" in html
    else:
        assert "/book/nova" in html


@pytest.fixture
async def shop(db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Cut, wash", price=2500, duration_minutes=60)
    master = models.Master(organization_id=org_a.org_id, full_name="Ann")
    db.add_all([service, master])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=master.id, day_of_week=d, start_time="09:00", end_time="18:00")
                for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=3)
    return SimpleNamespace(service=service.id, master=master.id, day=day)


async def test_online_booking_email_has_calendar_links(api, org_a, shop, sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "backend_url", "https://api.example.com")
    start = datetime.combine(shop.day, datetime.min.time()).replace(hour=10)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json={
        "client_full_name": "Olena Test", "client_phone": "+380671230077", "client_email": "olena@example.com",
        "service_id": shop.service, "master_id": shop.master, "start_time": start.isoformat()})
    assert r.status_code == 201, r.text
    html = sent_emails[-1]["html"]
    assert "calendar.google.com" in html and "calendar.ics" in html and "Olena" in html
    assert f"#{r.json()['id']}" in html and "€25" in html


async def test_ics_file(api, db, org_a, shop):
    org = await db.get(models.Organization, org_a.org_id)
    org.timezone, org.address = "Europe/Kyiv", "Main st. 1; Kyiv"
    start = datetime(2030, 7, 1, 10, 0)
    row = models.Appointment(organization_id=org_a.org_id, client_id=None, service_id=shop.service,
                             master_id=shop.master, start_time=start, end_time=start + timedelta(hours=1),
                             price=2500, currency="EUR")
    client = models.Client(organization_id=org_a.org_id, full_name="X", phone="+380671230078")
    db.add(client)
    await db.flush()
    row.client_id = client.id
    db.add(row)
    await db.flush()
    token = await create_token(db, row.id, "en")
    await db.commit()

    r = await api.get(f"{SLUG_BASE}/booking/{token}/calendar.ics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/calendar")
    body = r.text
    assert "DTSTART:20300701T070000Z" in body  # 10:00 в Киеве летом = 07:00 UTC
    assert "DTEND:20300701T080000Z" in body
    assert "SUMMARY:Cut\\, wash" in body and "LOCATION:Main st. 1\\; Kyiv" in body
    assert "STATUS:CONFIRMED" in body and "\r\n" in body

    await api.post(f"{SLUG_BASE}/booking/{token}/cancel")
    assert "STATUS:CANCELLED" in (await api.get(f"{SLUG_BASE}/booking/{token}/calendar.ics")).text
    assert (await api.get(f"{SLUG_BASE}/booking/wrong/calendar.ics")).status_code == 404
