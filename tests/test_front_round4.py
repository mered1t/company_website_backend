"""Запросы фронта от 20.10, вторая часть: «Сегодня» одним запросом, массовая смена статуса, счётчики меню."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from auth.auth import create_access_token
from services.common import get_org_now


@pytest.fixture
async def day(db, org_a):
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=30)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")
    born_today = None
    now = await get_org_now(db, org_a.org_id)
    born_today = date(1990, now.month, min(now.day, 28)) if now.day <= 28 else None
    olena = models.Client(organization_id=org_a.org_id, full_name="Olena", phone="+380670000901",
                          email="olena@example.com", birth_date=born_today)
    petro = models.Client(organization_id=org_a.org_id, full_name="Petro", phone="+380670000902")
    db.add_all([cut, ann, bob, olena, petro])
    await db.flush()
    today = datetime.combine(now.date(), datetime.min.time())
    hour = timedelta(hours=1)

    def appt(start, status="scheduled", master=ann, client=olena):
        return models.Appointment(organization_id=org_a.org_id, client_id=client.id, service_id=cut.id,
                                  master_id=master.id, start_time=start, end_time=start + timedelta(minutes=30),
                                  status=status, price=1000, currency="EUR")

    started = now.replace(second=0, microsecond=0) - hour   # уже началась сегодня
    later = now.replace(second=0, microsecond=0) + hour     # ещё впереди
    if started.date() != now.date():
        started = today
    if later.date() != now.date():
        later = today + timedelta(hours=23, minutes=20)
    rows = {
        "done": appt(today + timedelta(minutes=1), "completed"),
        "missed": appt(today + timedelta(minutes=2), "no_show", master=bob),
        "cancel": appt(today + timedelta(minutes=3), "cancelled"),
        "awaiting": appt(started, "scheduled", client=petro),
        "upcoming": appt(later, "scheduled", master=bob),
        "tomorrow": appt(today + timedelta(days=1, hours=10)),
        "old_unmarked": appt(today - timedelta(days=3) + 10 * hour, master=bob),
        "too_old": appt(today - timedelta(days=90) + 10 * hour),
        "old_done": appt(today - timedelta(days=2) + 10 * hour, "completed"),
    }
    db.add_all(rows.values())
    await db.commit()
    # около полуночи «ещё впереди» может уже начаться — ожидаемые числа считаем от этого
    ahead = 1 if later > now else 0
    return SimpleNamespace(ann=ann.id, bob=bob.id, born_today=born_today, ahead=ahead,
                           ids={k: v.id for k, v in rows.items()}, now=now)


async def _master(db, org, master_id):
    user = models.User(username=f"m{master_id}", email=f"m{master_id}@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole.master,
                             master_id=master_id))
    await db.commit()
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


# ------------------------------------------------------------------ 7: «Сегодня»
async def test_today_overview(api, org_a, day):
    r = await api.get(f"{org_a.base}/today", headers=org_a.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["date"] == day.now.date().isoformat()
    ids = [a["id"] for a in body["today"]]
    assert set(ids) == {day.ids[k] for k in ("done", "missed", "cancel", "awaiting", "upcoming")}
    assert body["today"][0]["client_name"] and body["today"][0]["service_name"] == "Cut"
    assert body["summary"] == {"total": 4, "completed": 1, "no_show": 1, "awaiting": 2 - day.ahead,
                               "upcoming": day.ahead, "cancelled": 1, "earned": 1000, "expected": 2000}
    assert body["tomorrow"]["count"] == 1 and body["tomorrow"]["items"][0]["id"] == day.ids["tomorrow"]
    assert body["unmarked"] == {"total": 1, "items": body["unmarked"]["items"]}
    assert body["unmarked"]["items"][0]["id"] == day.ids["old_unmarked"]
    if day.born_today:
        assert [b["full_name"] for b in body["birthdays"]] == ["Olena"]


async def test_today_for_a_master(api, db, org_a, day):
    headers = await _master(db, org_a, day.bob)
    body = (await api.get(f"{org_a.base}/today", headers=headers)).json()
    assert {a["id"] for a in body["today"]} == {day.ids["missed"], day.ids["upcoming"]}
    assert body["summary"]["total"] == 2 and body["tomorrow"]["count"] == 0
    assert body["unmarked"]["total"] == 1 and body["birthdays"] is None


# ------------------------------------------------------------------ 9: счётчики
async def test_counters(api, db, org_a, org_b, day):
    r = await api.get(f"{org_a.base}/counters", headers=org_a.headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"today": 4, "unmarked": 1, "clients": 2, "masters": 2, "services": 1, "waitlist": 0,
                        "notifications_unread": 0}
    headers = await _master(db, org_a, day.ann)
    mine = (await api.get(f"{org_a.base}/counters", headers=headers)).json()
    assert mine["today"] == 2 and mine["unmarked"] == 0 and mine["waitlist"] is None
    other = (await api.get(f"{org_b.base}/counters", headers=org_b.headers)).json()
    assert other["clients"] == 0 and other["today"] == 0


# ------------------------------------------------------------------ 8: «Все пришли»
async def test_bulk_status(api, db, org_a, day, sent_emails):
    url = f"{org_a.base}/appointments/bulk-status"
    ids = [day.ids["awaiting"], day.ids["old_unmarked"], day.ids["upcoming"], 999999, day.ids["awaiting"]]
    r = await api.post(url, headers=org_a.headers, json={"ids": ids, "status": "completed"})
    assert r.status_code == 200, r.text
    body = r.json()
    # владелец может закрыть и будущую запись (как в PATCH); несуществующая — в failed
    assert body["updated"] == [day.ids["awaiting"], day.ids["old_unmarked"], day.ids["upcoming"]]
    assert body["failed"] == [{"id": 999999, "detail": "Appointment not found"}]
    counters = (await api.get(f"{org_a.base}/counters", headers=org_a.headers)).json()
    assert counters["unmarked"] == 0

    assert (await api.post(url, headers=org_a.headers, json={"ids": [], "status": "completed"})).status_code == 422
    too_many = {"ids": list(range(1, 102)), "status": "completed"}
    assert (await api.post(url, headers=org_a.headers, json=too_many)).status_code == 422


async def test_bulk_status_follows_master_rules(api, db, org_a, day):
    headers = await _master(db, org_a, day.bob)
    url = f"{org_a.base}/appointments/bulk-status"
    r = await api.post(url, headers=headers, json={
        "ids": [day.ids["upcoming"], day.ids["old_unmarked"], day.ids["awaiting"]], "status": "no_show"})
    body = r.json()
    failed = {f["id"]: f["detail"] for f in body["failed"]}
    if day.ahead:
        assert body["updated"] == [day.ids["old_unmarked"]]  # своя и уже прошла
        assert "once the appointment has started" in failed[day.ids["upcoming"]]   # ещё не началась
    assert "own appointments" in failed[day.ids["awaiting"]]                   # чужая (мастер Ann)


async def test_bulk_cancel_emails_clients(api, org_a, day, sent_emails):
    r = await api.post(f"{org_a.base}/appointments/bulk-status", headers=org_a.headers,
                       json={"ids": [day.ids["tomorrow"]], "status": "cancelled"})
    assert r.json()["updated"] == [day.ids["tomorrow"]]
    assert [m["to"] for m in sent_emails] == ["olena@example.com"]
    sent_emails.clear()
    r = await api.post(f"{org_a.base}/appointments/bulk-status", headers=org_a.headers,
                       json={"ids": [day.ids["upcoming"]], "status": "cancelled", "notify_client": False})
    assert r.json()["updated"] == [day.ids["upcoming"]] and sent_emails == []


# ------------------------------------------------------------------ название салона проверяется как остальные имена
async def test_organization_name_is_validated(api, org_a):
    url = "/api/v1/organizations"
    assert (await api.post(url, headers=org_a.headers, json={})).status_code == 422
    assert (await api.post(url, headers=org_a.headers, json={"name": "   "})).status_code == 422
    assert (await api.post(url, headers=org_a.headers, json={"name": "x" * 151})).status_code == 422
    r = await api.post(url, headers=org_a.headers, json={"name": "  Studio Nova  "})
    assert r.status_code == 201 and r.json()["name"] == "Studio Nova"
