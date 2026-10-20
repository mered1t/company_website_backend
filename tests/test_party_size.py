"""Одна запись на компанию: у услуги max_party_size, у записи party_size. Слот занят один раз, цена — за слот."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from core.rate_limiter import limiter
from services.common import get_org_now
from services.booking_tokens import create_token
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
    tennis = models.Service(organization_id=org_a.org_id, name="Tennis", price=4000, duration_minutes=60,
                            max_party_size=4)
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    coach = models.Master(organization_id=org_a.org_id, full_name="Coach")
    client = models.Client(organization_id=org_a.org_id, full_name="Player", phone="+380670000201")
    db.add_all([tennis, cut, coach, client])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=coach.id, day_of_week=d, start_time="09:00", end_time="13:00")
                for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=2)
    return SimpleNamespace(tennis=tennis.id, cut=cut.id, coach=coach.id, client=client.id, day=day)


def _at(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


async def _crm(api, org, court, **body):
    return await api.post(f"{org.base}/appointments", headers=org.headers, json={
        "client_id": court.client, "service_id": court.tennis, "master_id": court.coach,
        "start_time": _at(court.day, 10).isoformat(), **body})


async def _public(api, org, court, phone="+380671230011", **body):
    return await api.post(f"{SLUG_BASE}/{org.slug}/book", json={
        "client_full_name": "Online", "client_phone": phone, "service_id": court.tennis,
        "master_id": court.coach, "start_time": _at(court.day, 10).isoformat(), **body})


async def test_crm_books_a_party_with_one_price(api, org_a, court):
    r = await _crm(api, org_a, court, party_size=2)
    assert r.status_code == 201, r.text
    assert r.json()["party_size"] == 2 and r.json()["price"] == 4000
    default = await _crm(api, org_a, court, start_time=_at(court.day, 12).isoformat())
    assert default.json()["party_size"] == 1


async def test_party_bigger_than_the_service_is_refused(api, org_a, court):
    r = await _crm(api, org_a, court, party_size=5)
    assert r.status_code == 400 and "at most 4 people" in r.json()["detail"]
    r = await _crm(api, org_a, court, service_id=court.cut, party_size=2)
    assert r.status_code == 400 and "at most 1 person" in r.json()["detail"]
    for bad in (0, 51):
        assert (await _crm(api, org_a, court, party_size=bad)).status_code == 422


async def test_the_slot_belongs_to_the_party(api, org_a, court):
    assert (await _crm(api, org_a, court, party_size=2)).status_code == 201
    r = await _public(api, org_a, court, party_size=1)
    assert r.status_code == 400 and r.json()["detail"] == MASTER_BUSY
    slots = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-slots",
                          params={"service_id": court.tennis, "date": court.day.isoformat(), "master_id": court.coach})
    assert all(s["start_time"][11:16] != "10:00" for s in slots.json())


async def test_patch_party_size_and_service(api, org_a, court):
    appointment = (await _crm(api, org_a, court, party_size=3)).json()["id"]
    url = f"{org_a.base}/appointments/{appointment}"
    assert (await api.patch(url, headers=org_a.headers, json={"party_size": 4})).json()["party_size"] == 4
    assert (await api.patch(url, headers=org_a.headers, json={"party_size": 5})).status_code == 400
    assert (await api.patch(url, headers=org_a.headers, json={"party_size": None})).status_code == 422
    to_cut = await api.patch(url, headers=org_a.headers, json={"service_id": court.cut})
    assert to_cut.status_code == 400  # компания из 4 человек не помещается в услугу на одного
    both = await api.patch(url, headers=org_a.headers, json={"service_id": court.cut, "party_size": 1})
    assert both.status_code == 200, both.text


async def test_public_booking_is_for_one_person(api, db, org_a, court):
    """Компании записывает только салон в CRM; онлайн — всегда один человек."""
    for extra in ({"party_size": 2}, {"guest_names": ["Friend"]}, {"open_capacity": 4}):
        assert (await _public(api, org_a, court, **extra)).status_code == 422
    r = await _crm(api, org_a, court, party_size=2)
    assert r.status_code == 201, r.text
    token = await create_token(db, r.json()["id"], "en")
    await db.commit()
    info = await api.get(f"{SLUG_BASE}/booking/{token}")
    assert info.json()["party_size"] == 2


async def test_service_field(api, org_a):
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "Court", "price": 3000, "duration_minutes": 60, "max_party_size": 4})
    assert r.status_code == 201 and r.json()["max_party_size"] == 4
    url = f"{org_a.base}/services/{r.json()['id']}"
    for bad in (0, 51, None):
        assert (await api.patch(url, headers=org_a.headers, json={"max_party_size": bad})).status_code == 422
    public = await api.get(f"{SLUG_BASE}/{org_a.slug}/services")
    assert public.json()[0]["max_party_size"] == 4
