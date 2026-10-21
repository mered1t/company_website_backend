"""Запросы фронта от 20.10: свободное время для CRM, total_spent и no_shows, порядок истории, схема листа ожидания."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from auth.auth import create_access_token
from core.rate_limiter import limiter
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE


@pytest.fixture
async def shop(db, org_a):
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1500, duration_minutes=60)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")
    client = models.Client(organization_id=org_a.org_id, full_name="Olena", phone="+380670000801")
    db.add_all([cut, ann, bob, client])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=m.id, day_of_week=d, start_time="10:00", end_time="13:00")
                for m in (ann, bob) for d in range(7)])
    await db.commit()
    today = (await get_org_now(db, org_a.org_id)).date()
    return SimpleNamespace(cut=cut.id, ann=ann.id, bob=bob.id, client=client.id, today=today)


def _at(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


def _appt(org, shop, start, status="completed", master=None, price=1500):
    return models.Appointment(organization_id=org.org_id, client_id=shop.client, service_id=shop.cut,
                              master_id=master or shop.ann, start_time=start, end_time=start + timedelta(hours=1),
                              status=status, price=price, currency="EUR")


async def _master_headers(db, org, master_id):
    user = models.User(username=f"m{master_id}", email=f"m{master_id}@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole.master,
                             master_id=master_id))
    await db.commit()
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


# ------------------------------------------------------------------ 10: свободное время для CRM
async def test_crm_slots_without_public_limits(api, db, org_a, shop):
    far = shop.today + timedelta(days=200)  # дальше горизонта записи (30 дней)
    db.add(_appt(org_a, shop, _at(far, 11), status="scheduled"))
    await db.commit()
    url = f"{org_a.base}/available-slots"
    r = await api.get(url, headers=org_a.headers, params={"service_id": shop.cut, "date": far.isoformat()})
    assert r.status_code == 200, r.text
    slots = {s["start_time"][11:16]: s["master_ids"] for s in r.json()}
    assert slots["11:00"] == [shop.bob]  # Ann занята в 11:00
    assert sorted(slots["10:00"]) == sorted([shop.ann, shop.bob])

    one = await api.get(url, headers=org_a.headers,
                        params={"service_id": shop.cut, "date": far.isoformat(), "master_id": shop.ann})
    assert [s["start_time"][11:16] for s in one.json()] == ["10:00", "12:00"]

    previous = limiter.enabled
    limiter.enabled = True
    try:
        for _ in range(40):  # публичный лимит 30 в минуту, здесь его нет
            assert (await api.get(url, headers=org_a.headers,
                                  params={"service_id": shop.cut, "date": far.isoformat()})).status_code == 200
    finally:
        limiter.enabled = previous
    assert (await api.get(url, params={"service_id": shop.cut, "date": far.isoformat()})).status_code == 401


async def test_crm_slots_for_rescheduling_and_past_days(api, db, org_a, shop):
    booked = _appt(org_a, shop, _at(shop.today + timedelta(days=1), 11), status="scheduled")
    db.add(booked)
    await db.commit()
    url = f"{org_a.base}/available-slots"
    day = (shop.today + timedelta(days=1)).isoformat()
    base = {"service_id": shop.cut, "date": day, "master_id": shop.ann}
    without = [s["start_time"][11:16] for s in (await api.get(url, headers=org_a.headers, params=base)).json()]
    assert "11:00" not in without
    moving = await api.get(url, headers=org_a.headers, params={**base, "exclude_appointment_id": booked.id})
    assert "11:00" in [s["start_time"][11:16] for s in moving.json()]

    past = await api.get(url, headers=org_a.headers,
                         params={"service_id": shop.cut, "date": (shop.today - timedelta(days=3)).isoformat()})
    assert len(past.json()) == 9  # прошлый день целиком (шаг 15 мин): визит можно внести задним числом

    assert (await api.get(url, headers=org_a.headers, params={**base, "exclude_appointment_id": 999999})).status_code == 404


async def test_master_sees_only_own_slots(api, db, org_a, shop):
    headers = await _master_headers(db, org_a, shop.bob)
    r = await api.get(f"{org_a.base}/available-slots", headers=headers,
                      params={"service_id": shop.cut, "date": (shop.today + timedelta(days=1)).isoformat(),
                              "master_id": shop.ann})
    assert r.status_code == 200 and all(s["master_ids"] == [shop.bob] for s in r.json())


# ------------------------------------------------------------------ 3 и 11: потратил и неявки
async def test_total_spent_and_no_shows(api, db, org_a, shop):
    past = shop.today - timedelta(days=10)
    db.add_all([
        _appt(org_a, shop, _at(past, 10), price=1500),
        _appt(org_a, shop, _at(past + timedelta(days=1), 10), price=2500),
        _appt(org_a, shop, _at(past + timedelta(days=2), 10), status="no_show"),
        _appt(org_a, shop, _at(past + timedelta(days=3), 10), status="cancelled"),
    ])
    await db.commit()
    one = (await api.get(f"{org_a.base}/clients/{shop.client}", headers=org_a.headers)).json()
    assert one["total_spent"] == 4000 and one["no_shows"] == 1 and one["visits"] == 2
    listed = (await api.get(f"{org_a.base}/clients", headers=org_a.headers)).json()
    assert listed[0]["total_spent"] == 4000 and listed[0]["no_shows"] == 1

    headers = await _master_headers(db, org_a, shop.ann)
    as_master = (await api.get(f"{org_a.base}/clients/{shop.client}", headers=headers)).json()
    assert as_master["total_spent"] is None and as_master["no_shows"] == 1  # деньги мастеру не показываем


# ------------------------------------------------------------------ 5: порядок истории
async def test_history_order(api, db, org_a, shop):
    days = [shop.today - timedelta(days=d) for d in (30, 20, 10)]
    db.add_all([_appt(org_a, shop, _at(d, 10)) for d in days])
    await db.commit()
    url = f"{org_a.base}/clients/{shop.client}/appointments"
    newest = [a["start_time"][:10] for a in (await api.get(url, headers=org_a.headers)).json()]
    assert newest == sorted(newest, reverse=True)
    oldest = [a["start_time"][:10] for a in (await api.get(url, headers=org_a.headers, params={"dir": "asc"})).json()]
    assert oldest == sorted(oldest)
    assert (await api.get(url, headers=org_a.headers, params={"dir": "up"})).status_code == 422


# ------------------------------------------------------------------ 4: лист ожидания
async def test_waitlist_has_names(api, db, org_a, shop):
    limiter.enabled, previous = False, limiter.enabled
    try:
        for master in (None, shop.bob):
            body = {"service_id": shop.cut, "day": (shop.today + timedelta(days=2)).isoformat(),
                    "client_full_name": "Waiter", "client_phone": f"+38067111000{1 if master else 2}",
                    "client_email": "w@example.com", "language": "uk"}
            if master:
                body["master_id"] = master
            assert (await api.post(f"{SLUG_BASE}/{org_a.slug}/waitlist", json=body)).status_code == 201
    finally:
        limiter.enabled = previous
    rows = (await api.get(f"{org_a.base}/waitlist", headers=org_a.headers)).json()
    assert {(r["service_name"], r["master_name"]) for r in rows} == {("Cut", None), ("Cut", "Bob")}
    assert set(rows[0]) == {"id", "service_id", "service_name", "master_id", "master_name", "day", "client_name",
                            "phone", "email", "language", "notified_at", "created_at"}
    from schemas.waitlist import WaitlistEntryPublic
    assert WaitlistEntryPublic.model_json_schema()["properties"]["notified_at"]["description"]
