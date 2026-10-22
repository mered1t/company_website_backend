"""Несколько клиентов в одной записи: основной клиент + participant_ids (клиенты из базы салона).

Цена — за человека: у записи сумма за всех, у каждого клиента своя часть в истории, визитах и тратах.
Слот мастера занят один раз. Добавляет только салон в CRM (мастер тоже); онлайн записывается один человек.
"""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from auth.auth import create_access_token
from core.rate_limiter import limiter
from services.booking_tokens import create_token
from services.common import get_org_now
from services.reminders import send_due_reminders
from tests.test_booking_manage import SLUG_BASE

MASTER_BUSY = "Master already has an appointment at this time"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
async def court(db, org_a):
    tennis = models.Service(organization_id=org_a.org_id, name="Tennis", price=1500, duration_minutes=60,
                            max_party_size=4)
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    coach = models.Master(organization_id=org_a.org_id, full_name="Coach")
    people = [models.Client(organization_id=org_a.org_id, full_name=name, phone=f"+38067000060{i}",
                            email=f"{name.lower()}@example.com" if name != "Dan" else None)
              for i, name in enumerate(["Anna", "Bob", "Carl", "Dan", "Eva"])]
    db.add_all([tennis, cut, coach, *people])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=coach.id, day_of_week=d, start_time="09:00", end_time="13:00")
                for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=3)
    anna, bob, carl, dan, eva = (c.id for c in people)
    return SimpleNamespace(tennis=tennis.id, cut=cut.id, coach=coach.id, day=day,
                           anna=anna, bob=bob, carl=carl, dan=dan, eva=eva)


def _at(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


async def _crm(api, org, court, headers=None, **body):
    return await api.post(f"{org.base}/appointments", headers=headers or org.headers, json={
        "client_id": court.anna, "service_id": court.tennis, "master_id": court.coach,
        "start_time": _at(court.day, 10).isoformat(), **body})


def _ids(appointment):
    return [p["client_id"] for p in appointment["participants"]]


async def test_add_clients_to_an_appointment(api, org_a, court):
    r = await _crm(api, org_a, court, participant_ids=[court.bob, court.carl, court.bob])  # повтор — один раз
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["party_size"] == 3 and body["price"] == 4500  # цена услуги за каждого
    assert body["participants"] == [
        {"client_id": court.bob, "client_name": "Bob", "client_phone": "+380670000601", "price": 1500},
        {"client_id": court.carl, "client_name": "Carl", "client_phone": "+380670000602", "price": 1500},
    ]
    single = (await _crm(api, org_a, court, start_time=_at(court.day, 12).isoformat())).json()
    assert single["party_size"] == 1 and single["participants"] == [] and single["price"] == 1500

    one = (await api.get(f"{org_a.base}/appointments/{body['id']}", headers=org_a.headers)).json()
    assert _ids(one) == [court.bob, court.carl]
    calendar = await api.get(f"{org_a.base}/appointments/calendar", headers=org_a.headers, params={
        "date_from": _at(court.day, 0).isoformat(), "date_to": _at(court.day, 23).isoformat()})
    assert _ids(calendar.json()[0]) == [court.bob, court.carl] and calendar.json()[0]["client_name"] == "Anna"


async def test_limits(api, db, org_a, org_b, court):
    too_many = await _crm(api, org_a, court, participant_ids=[court.bob, court.carl, court.dan, court.eva])
    assert too_many.status_code == 400 and "at most 4 people" in too_many.json()["detail"]
    one_person = await _crm(api, org_a, court, service_id=court.cut, participant_ids=[court.bob])
    assert one_person.status_code == 400 and "at most 1 person" in one_person.json()["detail"]
    itself = await _crm(api, org_a, court, participant_ids=[court.anna])
    assert itself.status_code == 400 and "already in it" in itself.json()["detail"]
    stranger = models.Client(organization_id=org_b.org_id, full_name="Stranger", phone="+380670000699")
    db.add(stranger)
    await db.commit()
    assert (await _crm(api, org_a, court, participant_ids=[stranger.id])).status_code == 404
    assert (await _crm(api, org_a, court, participant_ids=list(range(1, 51)))).status_code == 422
    assert (await _crm(api, org_a, court, participant_ids=[court.bob])).status_code == 201


async def test_the_slot_belongs_to_the_group(api, org_a, court):
    assert (await _crm(api, org_a, court, participant_ids=[court.bob])).status_code == 201
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json={
        "client_full_name": "Online", "client_phone": "+380671230011", "service_id": court.tennis,
        "master_id": court.coach, "start_time": _at(court.day, 10).isoformat()})
    assert r.status_code == 400 and r.json()["detail"] == MASTER_BUSY


async def test_patch_the_list(api, org_a, court):
    appointment = (await _crm(api, org_a, court, participant_ids=[court.bob, court.carl])).json()["id"]
    url = f"{org_a.base}/appointments/{appointment}"
    r = await api.patch(url, headers=org_a.headers, json={"participant_ids": [court.carl, court.dan]})
    assert r.status_code == 200, r.text
    assert _ids(r.json()) == [court.carl, court.dan] and r.json()["party_size"] == 3 and r.json()["price"] == 4500
    assert (await api.patch(url, headers=org_a.headers, json={"participant_ids": None})).status_code == 422

    # основного клиента нельзя сделать тем, кто уже в списке, пока его оттуда не убрали
    assert (await api.patch(url, headers=org_a.headers, json={"client_id": court.carl})).status_code == 400
    r = await api.patch(url, headers=org_a.headers, json={"client_id": court.carl, "participant_ids": [court.dan]})
    assert r.status_code == 200 and r.json()["client_id"] == court.carl and _ids(r.json()) == [court.dan]

    # услуга на одного не вмещает двоих; вместе с очисткой списка — можно, цена пересчитана
    assert (await api.patch(url, headers=org_a.headers, json={"service_id": court.cut})).status_code == 400
    r = await api.patch(url, headers=org_a.headers, json={"service_id": court.cut, "participant_ids": []})
    assert r.status_code == 200 and r.json()["party_size"] == 1 and r.json()["price"] == 1000
    activity = await api.get(f"{org_a.base}/activity", headers=org_a.headers)
    assert any("participant_ids" in (row["details"] or "") for row in activity.json())


async def test_service_change_reprices_everyone(api, db, org_a, court):
    pro = models.Service(organization_id=org_a.org_id, name="Pro", price=2500, duration_minutes=60, max_party_size=3)
    db.add(pro)
    await db.commit()
    appointment = (await _crm(api, org_a, court, participant_ids=[court.bob])).json()["id"]
    r = await api.patch(f"{org_a.base}/appointments/{appointment}", headers=org_a.headers,
                        json={"service_id": pro.id})
    assert r.status_code == 200 and r.json()["price"] == 5000 and r.json()["participants"][0]["price"] == 2500


async def test_each_client_gets_the_visit_and_their_share(api, db, org_a, court):
    past = (await get_org_now(db, org_a.org_id)).date() - timedelta(days=5)
    r = await _crm(api, org_a, court, start_time=_at(past, 10).isoformat(), participant_ids=[court.bob, court.carl])
    appointment = r.json()["id"]
    r = await api.patch(f"{org_a.base}/appointments/{appointment}", headers=org_a.headers,
                        json={"status": "completed"})
    assert r.status_code == 200, r.text

    for client_id in (court.anna, court.bob, court.carl):
        card = (await api.get(f"{org_a.base}/clients/{client_id}", headers=org_a.headers)).json()
        assert card["visits"] == 1 and card["total_spent"] == 1500 and card["last_visit"], card
        history = (await api.get(f"{org_a.base}/clients/{client_id}/appointments", headers=org_a.headers)).json()
        assert [a["id"] for a in history] == [appointment]
    assert (await api.get(f"{org_a.base}/clients/{court.dan}", headers=org_a.headers)).json()["visits"] == 0

    by_visits = (await api.get(f"{org_a.base}/clients", headers=org_a.headers,
                               params={"sort": "visits", "dir": "desc"})).json()
    assert {c["full_name"] for c in by_visits[:3]} == {"Anna", "Bob", "Carl"}
    summary = (await api.get(f"{org_a.base}/clients/summary", headers=org_a.headers)).json()
    assert summary["returning"] == 3 and summary["new"] == 2
    top = (await api.get(f"{org_a.base}/analytics/top-clients", headers=org_a.headers)).json()
    assert {(c["full_name"], c["total_spent"]) for c in top} == {("Anna", 1500), ("Bob", 1500), ("Carl", 1500)}


async def test_master_adds_clients_to_own_appointment(api, db, org_a, court):
    user = models.User(username="coach", email="coach@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org_a.org_id, role=models.MembershipRole.master,
                             master_id=court.coach))
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    r = await _crm(api, org_a, court, headers=headers, participant_ids=[court.bob])
    assert r.status_code == 201, r.text
    r = await api.patch(f"{org_a.base}/appointments/{r.json()['id']}", headers=headers,
                        json={"participant_ids": [court.bob, court.carl]})
    assert r.status_code == 200 and r.json()["party_size"] == 3


async def test_emails_to_the_other_clients(api, db, org_a, court, sent_emails):
    # напоминание: основному клиенту со ссылкой управления, остальным — без неё (у Dan нет email)
    soon = (await get_org_now(db, org_a.org_id)).replace(minute=0, second=0, microsecond=0) + timedelta(hours=3)
    row = models.Appointment(  # в базу напрямую: «через 3 часа» может выпасть из рабочего времени мастера
        organization_id=org_a.org_id, client_id=court.anna, service_id=court.tennis, master_id=court.coach,
        start_time=soon, end_time=soon + timedelta(hours=1), price=4500, currency="EUR", party_size=3,
        participants=[models.AppointmentParticipant(organization_id=org_a.org_id, client_id=c, price=1500)
                      for c in (court.bob, court.dan)])
    db.add(row)
    await db.commit()
    appointment = row.id
    sent_emails.clear()
    assert await send_due_reminders(db) == 2
    mails = {m["to"]: m for m in sent_emails}
    assert set(mails) == {"anna@example.com", "bob@example.com"}
    assert "/booking/" in mails["anna@example.com"]["html"]
    bob_html = mails["bob@example.com"]["html"]
    assert "/booking/" not in bob_html and "See you soon, Bob" in bob_html and "let the salon know" in bob_html
    sent_emails.clear()
    assert await send_due_reminders(db) == 0  # второй раз не шлём

    # перенос салоном: письмо «запись изменена» и основному, и остальным; только что добавленному — нет
    url = f"{org_a.base}/appointments/{appointment}"
    later = (soon + timedelta(days=1)).replace(hour=10)
    r = await api.patch(url, headers=org_a.headers, json={"start_time": later.isoformat(),
                                                          "participant_ids": [court.bob, court.carl]})
    assert r.status_code == 200, r.text
    assert sorted(m["to"] for m in sent_emails) == ["anna@example.com", "bob@example.com"]
    bob_mail = next(m for m in sent_emails if m["to"] == "bob@example.com")
    assert "has been changed" in bob_mail["html"] and "/booking/" not in bob_mail["html"]
    anna_mail = next(m for m in sent_emails if m["to"] == "anna@example.com")
    assert "€15" in bob_mail["html"] and "€15" in anna_mail["html"]  # у каждого своя часть, а не €45 за всех

    # отмена салоном: всем, у кого есть email
    sent_emails.clear()
    r = await api.patch(url, headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200
    assert sorted(m["to"] for m in sent_emails) == ["anna@example.com", "bob@example.com", "carl@example.com"]
    carl_mail = next(m for m in sent_emails if m["to"] == "carl@example.com")
    assert "was cancelled. Here is" in carl_mail["html"]


async def test_main_client_cancels_online(api, db, org_a, court, sent_emails):
    r = await _crm(api, org_a, court, participant_ids=[court.bob])
    token = await create_token(db, r.json()["id"], "en")
    await db.commit()
    info = (await api.get(f"{SLUG_BASE}/booking/{token}")).json()
    assert info["party_size"] == 2 and info["price"] == 3000
    sent_emails.clear()
    assert (await api.post(f"{SLUG_BASE}/booking/{token}/cancel")).status_code == 200
    assert "bob@example.com" in [m["to"] for m in sent_emails]


async def test_client_with_upcoming_group_visit_cannot_be_deleted(api, org_a, court):
    await _crm(api, org_a, court, participant_ids=[court.bob])
    r = await api.delete(f"{org_a.base}/clients/{court.bob}", headers=org_a.headers)
    assert r.status_code == 400 and "upcoming appointments" in r.json()["detail"]


async def test_public_booking_is_for_one_person(api, org_a, court):
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json={
        "client_full_name": "Online", "client_phone": "+380671230012", "service_id": court.tennis,
        "master_id": court.coach, "start_time": _at(court.day, 11).isoformat(), "party_size": 2})
    assert r.status_code == 422


async def test_service_field(api, org_a):
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "Court", "price": 3000, "duration_minutes": 60, "max_party_size": 4})
    assert r.status_code == 201 and r.json()["max_party_size"] == 4 and "open_games" not in r.json()
    url = f"{org_a.base}/services/{r.json()['id']}"
    for bad in (0, 51, None):
        assert (await api.patch(url, headers=org_a.headers, json={"max_party_size": bad})).status_code == 422
    public = await api.get(f"{SLUG_BASE}/{org_a.slug}/services")
    assert public.json()[0]["max_party_size"] == 4
