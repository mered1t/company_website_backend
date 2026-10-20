"""Открытые игры: свободные места в записи занимают другие люди (теннис 2–4 и т.п.)."""
import re
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import models
from core.config import settings
from core.rate_limiter import limiter
from services.booking_tokens import create_token
from services.common import get_org_now
from services.email_service import render_email
from services.i18n import SUPPORTED_LANGUAGES
from services.i18n_games import GAME_KINDS
from services.reminders import send_due_reminders
from tests.test_booking_manage import SLUG_BASE

PRICE = 1500  # за человека


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
async def court(db, org_a):
    tennis = models.Service(organization_id=org_a.org_id, name="Tennis", price=PRICE, duration_minutes=60,
                            max_party_size=4, open_games=True)
    haircut = models.Service(organization_id=org_a.org_id, name="Haircut", price=2000, duration_minutes=60,
                             max_party_size=4)
    coach = models.Master(organization_id=org_a.org_id, full_name="Coach")
    host = models.Client(organization_id=org_a.org_id, full_name="Host Player", phone="+380670000401",
                         email="host@example.com")
    other = models.Client(organization_id=org_a.org_id, full_name="Other Host", phone="+380670000402")
    db.add_all([tennis, haircut, coach, host, other])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=coach.id, day_of_week=d, start_time="08:00", end_time="20:00")
                for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=3)
    return SimpleNamespace(tennis=tennis.id, haircut=haircut.id, coach=coach.id, host=host.id, other=other.id,
                           day=day)


def _at(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


async def _book(api, org, court, **body):
    """Открытую игру создаёт салон в CRM (онлайн-запись всегда на одного человека)."""
    return await api.post(f"{org.base}/appointments", headers=org.headers, json={
        "client_id": court.host, "service_id": court.tennis, "master_id": court.coach,
        "start_time": _at(court.day, 10).isoformat(), "open_capacity": 4, **body})


async def _join(api, org, code, phone="+380671230002", email="anna@example.com", name="Anna Player", **body):
    return await api.post(f"{SLUG_BASE}/{org.slug}/games/{code}/join", json={
        "client_full_name": name, "client_phone": phone, "client_email": email, "language": "en", **body})


def _spot_token(html: str) -> str:
    return re.search(r"/game/([A-Za-z0-9_-]{20,})", html).group(1)


# ------------------------------------------------------------------ настройки услуги
async def test_service_needs_two_places_for_open_games(api, org_a):
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "Solo", "price": 100, "duration_minutes": 30, "open_games": True})
    assert r.status_code == 400
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "Duo", "price": 100, "duration_minutes": 30, "max_party_size": 2,
                             "open_games": True})
    assert r.status_code == 201 and r.json()["open_games"] is True
    r = await api.patch(f"{org_a.base}/services/{r.json()['id']}", headers=org_a.headers, json={"max_party_size": 1})
    assert r.status_code == 400


# ------------------------------------------------------------------ организатор открывает игру
async def test_salon_opens_a_game(api, org_a, court):
    r = await _book(api, org_a, court, party_size=2)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["open_capacity"] == 4 and body["join_code"] and body["price"] == 2 * PRICE
    online = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json={
        "client_full_name": "Olena", "client_phone": "+380671230001", "service_id": court.tennis,
        "master_id": court.coach, "start_time": _at(court.day, 12).isoformat(), "open_capacity": 4})
    assert online.status_code == 422  # онлайн игру не открыть

    info = await api.get(f"{SLUG_BASE}/{org_a.slug}/games/{body['join_code']}")
    assert info.status_code == 200
    assert info.json()["spots_left"] == 2 and info.json()["can_join"] is True
    assert info.json()["price_per_person"] == PRICE and info.json()["players"] == 2


async def test_open_game_rules(api, org_a, court):
    assert (await _book(api, org_a, court, service_id=court.haircut)).status_code == 400  # услуга без игр
    assert (await _book(api, org_a, court, open_capacity=5)).status_code == 400  # больше max_party_size
    assert (await _book(api, org_a, court, party_size=3, open_capacity=2)).status_code == 400
    assert (await _book(api, org_a, court, open_capacity=1)).status_code == 422
    assert (await api.get(f"{SLUG_BASE}/{org_a.slug}/games/nope")).status_code == 404


async def test_games_list(api, org_a, court):
    code = (await _book(api, org_a, court)).json()["join_code"]
    full = await _book(api, org_a, court, client_id=court.other, party_size=4,
                       start_time=_at(court.day, 12).isoformat())
    assert full.status_code == 201
    games = (await api.get(f"{SLUG_BASE}/{org_a.slug}/games")).json()
    assert [g["code"] for g in games] == [code]  # полная игра в списке не показывается
    assert (await api.get(f"{SLUG_BASE}/{org_a.slug}/games",
                          params={"date": (court.day + timedelta(days=1)).isoformat()})).json() == []
    assert len((await api.get(f"{SLUG_BASE}/{org_a.slug}/games", params={"service_id": court.tennis})).json()) == 1


# ------------------------------------------------------------------ присоединиться и выйти
async def test_join_and_leave(api, db, org_a, court, sent_emails):
    booking = (await _book(api, org_a, court)).json()
    code = booking["join_code"]
    sent_emails.clear()

    assert (await _join(api, org_a, code, party_size=2)).status_code == 422  # онлайн входит один человек
    r = await _join(api, org_a, code)
    assert r.status_code == 201, r.text
    assert r.json()["players"] == 2 and r.json()["spots_left"] == 2
    to = {m["to"]: m for m in sent_emails}
    assert set(to) == {"anna@example.com", "host@example.com"}
    assert "You've joined the game" in to["anna@example.com"]["html"]
    assert "€15" in to["anna@example.com"]["html"]  # цена за человека
    assert "Anna" in to["host@example.com"]["html"]  # организатору: кто присоединился
    token = _spot_token(to["anna@example.com"]["html"])

    appointment = await api.get(f"{org_a.base}/appointments/{booking['id']}", headers=org_a.headers)
    assert appointment.json()["party_size"] == 2 and appointment.json()["price"] == 2 * PRICE
    bell = await api.get(f"{org_a.base}/notifications", headers=org_a.headers)
    assert "game_joined" in [n["type"] for n in bell.json()]
    assert any(n["client_name"] == "Anna Player" for n in bell.json() if n["type"] == "game_joined")

    spot = await api.get(f"{SLUG_BASE}/participation/{token}")
    assert spot.status_code == 200 and spot.json()["status"] == "joined" and spot.json()["can_leave"] is True
    ics = await api.get(f"{SLUG_BASE}/participation/{token}/calendar.ics")
    assert ics.status_code == 200 and "STATUS:CONFIRMED" in ics.text

    sent_emails.clear()
    r = await api.post(f"{SLUG_BASE}/participation/{token}/leave")
    assert r.status_code == 200 and r.json()["status"] == "left" and r.json()["game"]["spots_left"] == 3
    assert [m["to"] for m in sent_emails] == ["host@example.com"]
    assert (await api.post(f"{SLUG_BASE}/participation/{token}/leave")).status_code == 400
    after = await api.get(f"{org_a.base}/appointments/{booking['id']}", headers=org_a.headers)
    assert after.json()["party_size"] == 1 and after.json()["price"] == PRICE
    assert (await api.get(f"{SLUG_BASE}/participation/wrong-token")).status_code == 404


async def test_join_rules(api, org_a, court):
    code = (await _book(api, org_a, court, party_size=2)).json()["join_code"]
    assert (await _join(api, org_a, code, website="bot")).status_code == 400
    assert (await _join(api, org_a, code, phone="+380670000401")).status_code == 400  # сам организатор
    assert (await _join(api, org_a, code)).status_code == 201
    assert (await _join(api, org_a, code)).status_code == 400  # второй раз тот же номер
    assert (await _join(api, org_a, code, phone="+380671230003", email=None)).status_code == 201  # без email можно
    game = (await api.get(f"{SLUG_BASE}/{org_a.slug}/games/{code}")).json()
    assert game["spots_left"] == 0 and game["can_join"] is False
    assert (await _join(api, org_a, code, phone="+380671230004")).status_code == 400  # мест нет


# ------------------------------------------------------------------ организатор или салон меняют игру
async def test_participants_hear_about_cancel_and_move(api, db, org_a, court, sent_emails):
    booking = (await _book(api, org_a, court)).json()
    await _join(api, org_a, booking["join_code"])
    url = f"{org_a.base}/appointments/{booking['id']}"

    sent_emails.clear()
    r = await api.patch(url, headers=org_a.headers, json={"start_time": _at(court.day, 14).isoformat()})
    assert r.status_code == 200, r.text
    anna = [m for m in sent_emails if m["to"] == "anna@example.com"]
    assert len(anna) == 1 and "The game was moved" in anna[0]["html"]

    # организатор отменяет по ссылке из своего письма
    token = await create_token(db, booking["id"], "uk", "host@example.com")
    await db.commit()
    sent_emails.clear()
    assert (await api.post(f"{SLUG_BASE}/booking/{token}/cancel")).status_code == 200
    anna = [m for m in sent_emails if m["to"] == "anna@example.com"]
    assert len(anna) == 1 and "The game is cancelled" in anna[0]["html"]


# ------------------------------------------------------------------ CRM
async def test_crm_open_game_and_participants(api, db, org_a, court, sent_emails):
    r = await api.post(f"{org_a.base}/appointments", headers=org_a.headers, json={
        "client_id": court.host, "service_id": court.tennis, "master_id": court.coach,
        "start_time": _at(court.day, 16).isoformat(), "open_capacity": 3})
    assert r.status_code == 201, r.text
    appointment = r.json()
    assert appointment["join_code"] and appointment["price"] == PRICE
    await _join(api, org_a, appointment["join_code"])
    url = f"{org_a.base}/appointments/{appointment['id']}"

    listed = await api.get(f"{url}/participants", headers=org_a.headers)
    assert listed.status_code == 200 and [p["client_name"] for p in listed.json()] == ["Anna Player"]
    participant = listed.json()[0]["id"]

    assert (await api.patch(url, headers=org_a.headers, json={"open_capacity": None})).status_code == 400
    assert (await api.patch(url, headers=org_a.headers, json={"party_size": 1})).status_code == 400
    r = await api.patch(url, headers=org_a.headers, json={"open_capacity": 4})
    assert r.status_code == 200 and r.json()["open_capacity"] == 4

    sent_emails.clear()
    r = await api.delete(f"{url}/participants/{participant}", headers=org_a.headers)
    assert r.status_code == 204
    assert [m["to"] for m in sent_emails] == ["anna@example.com"]
    assert "removed" in sent_emails[0]["html"].lower()
    assert (await api.delete(f"{url}/participants/{participant}", headers=org_a.headers)).status_code == 404
    assert len((await api.get(f"{url}/participants", headers=org_a.headers,
                              params={"include_left": True})).json()) == 1

    r = await api.patch(url, headers=org_a.headers, json={"open_capacity": None})
    assert r.status_code == 200 and r.json()["join_code"] is None and r.json()["price"] == PRICE
    r = await api.patch(url, headers=org_a.headers, json={"open_capacity": 2, "party_size": 2})
    assert r.status_code == 200 and r.json()["price"] == 2 * PRICE


# ------------------------------------------------------------------ напоминание участнику
async def test_participant_gets_a_reminder(api, db, org_a, court, sent_emails):
    soon = (await get_org_now(db, org_a.org_id)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    game = models.Appointment(
        organization_id=org_a.org_id, client_id=court.host, service_id=court.tennis, master_id=court.coach,
        start_time=soon, end_time=soon + timedelta(hours=1), price=PRICE, currency="EUR", open_capacity=4,
        join_code="soon-game-01", reminded_start_time=soon)
    db.add(game)
    await db.commit()
    assert (await _join(api, org_a, "soon-game-01")).status_code == 201
    participant = (await db.execute(select(models.AppointmentParticipant))).scalars().one()
    participant.created_at = participant.created_at - timedelta(days=2)  # вошёл задолго до игры
    await db.commit()

    sent_emails.clear()
    assert await send_due_reminders(db) == 1
    assert sent_emails[0]["to"] == "anna@example.com" and "Your game is coming up" in sent_emails[0]["html"]
    assert await send_due_reminders(db) == 0


# ------------------------------------------------------------------ данные клиента
async def test_anonymize_and_hard_delete_respect_participation(api, db, org_a, court):
    code = (await _book(api, org_a, court)).json()["join_code"]
    await _join(api, org_a, code)
    player = (await db.execute(select(models.Client.id).where(models.Client.phone == "+380671230002"))).scalar_one()
    assert (await api.post(f"{org_a.base}/clients/{player}/anonymize", headers=org_a.headers)).status_code == 204
    db.expire_all()
    spot = (await db.execute(select(models.AppointmentParticipant))).scalars().one()
    assert spot.email is None
    r = await api.delete(f"{org_a.base}/clients/{player}/permanent", headers=org_a.headers)
    assert r.status_code == 400


# ------------------------------------------------------------------ письма
@pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
@pytest.mark.parametrize("kind", GAME_KINDS)
def test_game_emails_render(kind, language, monkeypatch):
    monkeypatch.setattr(settings, "backend_url", "https://api.example.com")
    html = render_email("game_notice.html", language, kind=kind, organization_name="Club <b>X</b>",
                        service_name="Tennis", master_name="Coach", when="01.03.2030 10:00", players=3, capacity=4,
                        manage_url="https://app.example.com/game/tok", player_name="Anna")
    assert "Tennis" in html and "<b>X</b>" not in html
    assert not re.search(r"\{[a-z_]+\}", html)
