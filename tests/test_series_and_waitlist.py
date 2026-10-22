"""Повторяющиеся записи (серии) и лист ожидания."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import models
from core.rate_limiter import limiter
from services.booking_tokens import create_token
from services.cleanup import purge_old_rows
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
async def shop(db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Tennis", price=4000, duration_minutes=60,
                             max_party_size=2)
    coach = models.Master(organization_id=org_a.org_id, full_name="Coach")
    other = models.Master(organization_id=org_a.org_id, full_name="Other")
    clients = [models.Client(organization_id=org_a.org_id, full_name=f"C{i}", phone=f"+38067000050{i}",
                             email=f"c{i}@example.com") for i in range(3)]
    db.add_all([service, coach, other, *clients])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=m.id, day_of_week=d, start_time="10:00", end_time="12:00")
                for m in (coach, other) for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=2)
    return SimpleNamespace(service=service.id, coach=coach.id, other=other.id,
                           clients=[c.id for c in clients], day=day)


def _at(day, hour):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour)


async def _crm(api, org, shop, client=0, hour=10, day=None, master=None):
    return await api.post(f"{org.base}/appointments", headers=org.headers, json={
        "client_id": shop.clients[client], "service_id": shop.service, "master_id": master or shop.coach,
        "start_time": _at(day or shop.day, hour).isoformat()})


async def _series(api, org, shop, **extra):
    return await api.post(f"{org.base}/appointments/series", headers=org.headers, json={
        "client_id": shop.clients[0], "service_id": shop.service, "master_id": shop.coach,
        "start_time": _at(shop.day, 10).isoformat(), "count": 4, **extra})


# ------------------------------------------------------------------ серии
async def test_weekly_series_is_created(api, org_a, shop):
    r = await _series(api, org_a, shop, participant_ids=[shop.clients[1]])
    assert r.status_code == 201, r.text
    body = r.json()
    starts = [a["start_time"] for a in body["created"]]
    assert starts == [_at(shop.day + timedelta(weeks=i), 10).isoformat() for i in range(4)]
    assert body["skipped"] == [] and all(a["series_id"] == body["series_id"] for a in body["created"])
    assert all(a["party_size"] == 2 and a["price"] == 8000 and [p["client_id"] for p in a["participants"]]
               == [shop.clients[1]] for a in body["created"])
    listed = await api.get(f"{org_a.base}/appointments", headers=org_a.headers,
                           params={"series_id": body["series_id"]})
    assert listed.headers["x-total-count"] == "4"


async def test_every_two_weeks(api, org_a, shop):
    r = await _series(api, org_a, shop, repeat_every_weeks=2, count=3)
    assert [a["start_time"] for a in r.json()["created"]] == [
        _at(shop.day + timedelta(weeks=w), 10).isoformat() for w in (0, 2, 4)]


async def test_conflict_refuses_the_whole_series_by_default(api, org_a, shop):
    await _crm(api, org_a, shop, client=1, day=shop.day + timedelta(weeks=2))
    r = await _series(api, org_a, shop)
    assert r.status_code == 409
    conflicts = r.json()["detail"]["conflicts"]
    assert len(conflicts) == 1 and conflicts[0]["start_time"].startswith((shop.day + timedelta(weeks=2)).isoformat())
    listed = await api.get(f"{org_a.base}/appointments", headers=org_a.headers)
    assert listed.headers["x-total-count"] == "1"  # ничего из серии не создано


async def test_skip_conflicts_creates_the_rest(api, org_a, shop):
    await _crm(api, org_a, shop, client=1, day=shop.day + timedelta(weeks=1))
    r = await _series(api, org_a, shop, skip_conflicts=True)
    assert r.status_code == 201
    assert len(r.json()["created"]) == 3 and len(r.json()["skipped"]) == 1


async def test_series_limits(api, org_a, shop):
    assert (await _series(api, org_a, shop, count=1)).status_code == 422
    assert (await _series(api, org_a, shop, count=53)).status_code == 422
    assert (await _series(api, org_a, shop, repeat_every_weeks=5)).status_code == 422
    assert (await _series(api, org_a, shop, participant_ids=shop.clients[1:])).status_code == 400  # 3 > 2


async def test_cancel_the_rest_of_a_series(api, db, org_a, shop):
    body = (await _series(api, org_a, shop)).json()
    r = await api.post(f"{org_a.base}/appointments/series/{body['series_id']}/cancel", headers=org_a.headers)
    assert r.status_code == 200 and r.json() == {"cancelled": 4}
    again = await api.post(f"{org_a.base}/appointments/series/{body['series_id']}/cancel", headers=org_a.headers)
    assert again.json() == {"cancelled": 0}
    missing = await api.post(f"{org_a.base}/appointments/series/nope/cancel", headers=org_a.headers)
    assert missing.status_code == 404


# ------------------------------------------------------------------ лист ожидания
async def _join(api, org, shop, phone="+380671110001", email="wait@example.com", **extra):
    return await api.post(f"{SLUG_BASE}/{org.slug}/waitlist", json={
        "service_id": shop.service, "day": shop.day.isoformat(), "client_full_name": "Waiter",
        "client_phone": phone, "client_email": email, "language": "uk", **extra})


async def _fill_day(api, org, shop):
    """Оба мастера заняты весь рабочий день (10:00–12:00)."""
    ids = []
    for i, (master, hour) in enumerate(((shop.coach, 10), (shop.coach, 11), (shop.other, 10), (shop.other, 11))):
        r = await _crm(api, org, shop, client=i % 3, hour=hour, master=master)
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids


async def test_join_and_list_in_crm(api, org_a, shop):
    r = await _join(api, org_a, shop)
    assert r.status_code == 201, r.text
    again = await _join(api, org_a, shop)
    assert again.status_code == 400
    listed = await api.get(f"{org_a.base}/waitlist", headers=org_a.headers)
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert listed.json()[0]["email"] == "wait@example.com" and listed.headers["x-total-count"] == "1"
    entry = listed.json()[0]["id"]
    assert (await api.delete(f"{org_a.base}/waitlist/{entry}", headers=org_a.headers)).status_code == 204
    assert (await api.get(f"{org_a.base}/waitlist", headers=org_a.headers)).json() == []


async def test_join_rules(api, org_a, shop):
    assert (await _join(api, org_a, shop, website="bot")).status_code == 400
    past = (shop.day - timedelta(days=5)).isoformat()
    assert (await _join(api, org_a, shop, day=past)).status_code == 400
    far = (shop.day + timedelta(days=400)).isoformat()
    assert (await _join(api, org_a, shop, day=far)).status_code == 400
    assert (await _join(api, org_a, shop, email=None)).status_code == 422
    for i in range(3):
        r = await _join(api, org_a, shop, day=(shop.day + timedelta(days=i)).isoformat())
        assert r.status_code == 201, r.text
    assert (await _join(api, org_a, shop, day=(shop.day + timedelta(days=5)).isoformat())).status_code == 429


async def test_cancel_in_crm_emails_the_waiting_list_once(api, org_a, shop, sent_emails):
    ids = await _fill_day(api, org_a, shop)
    await _join(api, org_a, shop)
    await _join(api, org_a, shop, phone="+380671110002", email="coach-fan@example.com", master_id=shop.other)
    sent_emails.clear()

    r = await api.patch(f"{org_a.base}/appointments/{ids[0]}", headers=org_a.headers, json={"status": "cancelled"})
    assert r.status_code == 200
    waitlist_mail = [m for m in sent_emails if m["to"] in ("wait@example.com", "coach-fan@example.com")]
    assert [m["to"] for m in waitlist_mail] == ["wait@example.com"]  # освободился Coach, второй ждёт Other
    mail = waitlist_mail[0]
    assert "Звільнився час" in mail["html"] and f"/book/{org_a.slug}?service_id={shop.service}" in mail["html"]

    sent_emails.clear()
    await api.patch(f"{org_a.base}/appointments/{ids[1]}", headers=org_a.headers, json={"status": "cancelled"})
    assert [m["to"] for m in sent_emails if m["to"] == "wait@example.com"] == []  # письмо уходит один раз

    listed = await api.get(f"{org_a.base}/waitlist", headers=org_a.headers, params={"include_notified": True})
    notified = {e["email"]: e["notified_at"] is not None for e in listed.json()}
    assert notified == {"wait@example.com": True, "coach-fan@example.com": False}


async def test_delete_and_client_cancel_also_notify(api, db, org_a, shop, sent_emails):
    ids = await _fill_day(api, org_a, shop)
    await _join(api, org_a, shop, master_id=shop.other)
    sent_emails.clear()
    assert (await api.delete(f"{org_a.base}/appointments/{ids[2]}", headers=org_a.headers)).status_code == 204
    assert [m["to"] for m in sent_emails] == ["wait@example.com"]

    await _join(api, org_a, shop, phone="+380671110003", email="second@example.com", master_id=shop.coach)
    token = await create_token(db, ids[0], "en")
    await db.commit()
    sent_emails.clear()
    assert (await api.post(f"{SLUG_BASE}/booking/{token}/cancel")).status_code == 200
    assert "second@example.com" in [m["to"] for m in sent_emails]


async def test_no_email_while_the_day_is_still_full(api, org_a, shop, sent_emails):
    ids = await _fill_day(api, org_a, shop)
    await _join(api, org_a, shop)
    sent_emails.clear()
    r = await api.patch(f"{org_a.base}/appointments/{ids[0]}", headers=org_a.headers, json={"notes": "hi"})
    assert r.status_code == 200
    assert sent_emails == []


async def test_old_entries_are_cleaned(db, org_a, shop):
    db.add(models.WaitlistEntry(organization_id=org_a.org_id, service_id=shop.service, day=shop.day - timedelta(days=5),
                                client_name="Old", phone="+380671119999", email="old@example.com"))
    await db.commit()
    assert (await purge_old_rows(db))["waitlist_entries"] == 1
    assert (await db.execute(select(models.WaitlistEntry))).scalars().all() == []
