"""Пункты 12, 14, 15 из списка фронта и журнал действий только с реально изменёнными полями."""
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import models
from services.common import get_org_now

AN = "/analytics"


@pytest.fixture
async def shop(db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")
    clients = [models.Client(organization_id=org_a.org_id, full_name=f"Client {i}", phone=f"+38067000070{i}",
                             email=f"c{i}@example.com") for i in range(4)]
    db.add_all([service, ann, bob, *clients])
    await db.flush()
    # Ann: пн–пт 10:00–14:00 (4 ч), Bob: пн 10:00–12:00
    db.add_all([models.WorkingHours(master_id=ann.id, day_of_week=d, start_time="10:00", end_time="14:00")
                for d in range(5)])
    db.add(models.WorkingHours(master_id=bob.id, day_of_week=0, start_time="10:00", end_time="12:00"))
    await db.commit()
    return SimpleNamespace(service=service.id, ann=ann.id, bob=bob.id, clients=[c.id for c in clients],
                           client_objs=clients)


def _appt(org, shop, client, master, start, status="completed", price=1000, minutes=60):
    return models.Appointment(organization_id=org.org_id, client_id=shop.clients[client], service_id=shop.service,
                              master_id=master, start_time=start, end_time=start + timedelta(minutes=minutes),
                              status=status, price=price, currency="EUR")


# ------------------------------------------------------------------ 14: визиты клиента
async def test_clients_have_visits_and_last_visit(api, db, org_a, shop):
    db.add_all([
        _appt(org_a, shop, 0, shop.ann, datetime(2025, 1, 6, 10)),
        _appt(org_a, shop, 0, shop.ann, datetime(2025, 2, 3, 10)),
        _appt(org_a, shop, 0, shop.ann, datetime(2025, 3, 3, 10), status="cancelled"),
        _appt(org_a, shop, 1, shop.ann, datetime(2025, 1, 7, 10)),
    ])
    await db.commit()
    rows = (await api.get(f"{org_a.base}/clients", headers=org_a.headers)).json()
    stats = {c["full_name"]: (c["visits"], c["last_visit"]) for c in rows}
    assert stats["Client 0"] == (2, "2025-02-03T10:00:00")
    assert stats["Client 1"] == (1, "2025-01-07T10:00:00")
    assert stats["Client 2"] == (0, None)
    one = (await api.get(f"{org_a.base}/clients/{shop.clients[0]}", headers=org_a.headers)).json()
    assert one["visits"] == 2 and one["last_visit"] == "2025-02-03T10:00:00"
    by_visits = await api.get(f"{org_a.base}/clients", headers=org_a.headers, params={"sort": "visits", "dir": "desc"})
    assert [c["full_name"] for c in by_visits.json()][:2] == ["Client 0", "Client 1"]


async def test_clients_summary_groups(api, db, org_a, shop):
    start = datetime(2025, 1, 6, 10)
    db.add_all([_appt(org_a, shop, 0, shop.ann, start + timedelta(days=7 * i)) for i in range(3)])  # постоянный
    db.add(_appt(org_a, shop, 1, shop.ann, start + timedelta(days=1)))  # вернувшийся
    await db.commit()
    r = await api.get(f"{org_a.base}/clients/summary", headers=org_a.headers)
    assert r.status_code == 200, r.text
    assert r.json() == {"total": 4, "new": 2, "returning": 1, "regular": 1}


# ------------------------------------------------------------------ 15: перенос без письма
async def _future(db, org_a, shop):
    now = await get_org_now(db, org_a.org_id)
    day = now.date() + timedelta(days=7 - now.weekday())  # следующий понедельник
    row = _appt(org_a, shop, 0, shop.ann, datetime.combine(day, datetime.min.time()).replace(hour=10),
                status="scheduled")
    db.add(row)
    await db.commit()
    return row.id, day


async def test_move_without_email(api, db, org_a, shop, sent_emails):
    appointment, day = await _future(db, org_a, shop)
    url = f"{org_a.base}/appointments/{appointment}"
    new_time = datetime.combine(day, datetime.min.time()).replace(hour=11).isoformat()
    r = await api.patch(url, headers=org_a.headers, json={"start_time": new_time, "notify_client": False})
    assert r.status_code == 200, r.text
    assert r.json()["start_time"] == new_time and sent_emails == []
    later = datetime.combine(day, datetime.min.time()).replace(hour=12).isoformat()
    r = await api.patch(url, headers=org_a.headers, json={"start_time": later})
    assert r.status_code == 200 and len(sent_emails) == 1  # по умолчанию письмо уходит
    r = await api.patch(url, headers=org_a.headers, json={"status": "cancelled", "notify_client": False})
    assert r.status_code == 200 and len(sent_emails) == 1
    assert (await api.patch(url, headers=org_a.headers, json={"notify_client": None})).status_code == 422


# ------------------------------------------------------------------ журнал: только изменённые поля
async def _client_log(db, client_id):
    rows = await db.execute(
        select(models.ActivityLog.details).where(models.ActivityLog.entity_type == "client",
                                                 models.ActivityLog.entity_id == client_id,
                                                 models.ActivityLog.action == "updated"),
    )
    return rows.scalars().all()


async def test_activity_log_lists_only_changed_fields(api, db, org_a, shop):
    client = shop.clients[0]
    form = {"full_name": "Client 0", "phone": "+380670000700", "email": "c0@example.com", "notes": None,
            "birth_date": None}
    r = await api.patch(f"{org_a.base}/clients/{client}", headers=org_a.headers, json={**form, "full_name": "Renamed"})
    assert r.status_code == 200, r.text
    assert await _client_log(db, client) == ["Updated fields: full_name"]
    r = await api.patch(f"{org_a.base}/clients/{client}", headers=org_a.headers, json={**form, "full_name": "Renamed"})
    assert r.status_code == 200
    assert await _client_log(db, client) == ["Updated fields: full_name"]  # ничего не поменялось — записи нет


async def test_birth_date_and_service_compare_by_value(api, db, org_a, shop):
    client = shop.clients[1]
    url = f"{org_a.base}/clients/{client}"
    await api.patch(url, headers=org_a.headers, json={"birth_date": "1990-05-01"})
    await api.patch(url, headers=org_a.headers, json={"birth_date": "1990-05-01", "full_name": "Client 1"})
    assert await _client_log(db, client) == ["Updated fields: birth_date"]

    service_url = f"{org_a.base}/services/{shop.service}"
    r = await api.patch(service_url, headers=org_a.headers,
                        json={"name": "Cut", "price": 1500, "duration_minutes": 60, "emoji": None})
    assert r.status_code == 200
    rows = await db.execute(select(models.ActivityLog.details).where(models.ActivityLog.entity_type == "service"))
    assert rows.scalars().all() == ["Updated fields: price"]


async def test_master_can_resend_unchanged_closed_fields(api, db, org_a, shop):
    """Форма целиком у закрытой прошлой записи: неизменённые статус и время больше не дают 403."""
    from auth.auth import create_access_token
    user = models.User(username="ann-form", email="ann-form@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org_a.org_id, role=models.MembershipRole.master,
                             master_id=shop.ann))
    past = datetime(2025, 1, 6, 10)
    row = _appt(org_a, shop, 0, shop.ann, past)
    db.add(row)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}
    r = await api.patch(f"{org_a.base}/appointments/{row.id}", headers=headers,
                        json={"status": "completed", "start_time": past.isoformat(), "notes": "paid cash"})
    assert r.status_code == 200, r.text
    r = await api.patch(f"{org_a.base}/appointments/{row.id}", headers=headers, json={"status": "cancelled"})
    assert r.status_code == 403


# ------------------------------------------------------------------ 12: аналитика мастеров и статусов
async def test_masters_workload_details(api, db, org_a, shop):
    # неделя 2025-01-06 (пн) … 2025-01-12; Ann по графику 5 × 4 = 20 ч, минус отгул в среду = 16 ч,
    # плюс особые часы в субботу 10:00–12:00 = 18 ч
    db.add(models.TimeOff(master_id=shop.ann, start_date=datetime(2025, 1, 8), end_date=datetime(2025, 1, 8)))
    db.add(models.WorkingHoursException(master_id=shop.ann, date=datetime(2025, 1, 11), start_time="10:00",
                                        end_time="12:00"))
    db.add_all([
        _appt(org_a, shop, 0, shop.ann, datetime(2025, 1, 6, 10), price=1000),
        _appt(org_a, shop, 1, shop.ann, datetime(2025, 1, 6, 11), price=2000),
        _appt(org_a, shop, 2, shop.ann, datetime(2025, 1, 7, 10), status="no_show"),
        _appt(org_a, shop, 3, shop.ann, datetime(2025, 1, 7, 12), status="cancelled"),
        _appt(org_a, shop, 0, shop.ann, datetime(2025, 1, 9, 10), status="scheduled", minutes=120),
    ])
    await db.commit()
    period = {"date_from": "2025-01-06T00:00:00", "date_to": "2025-01-12T23:59:59"}
    r = await api.get(f"{org_a.base}{AN}/masters-workload", headers=org_a.headers, params=period)
    assert r.status_code == 200, r.text
    ann = next(m for m in r.json() if m["master_id"] == shop.ann)
    assert ann["appointments_count"] == 2 and ann["no_show"] == 1 and ann["cancelled"] == 1
    assert ann["total_revenue"] == 3000 and ann["average_check"] == 1500
    assert ann["no_show_rate_percent"] == 33.3
    assert ann["booked_hours"] == 5.0 and ann["available_hours"] == 18.0 and ann["load_percent"] == 27.8
    bob = next(m for m in r.json() if m["master_id"] == shop.bob)
    assert bob["available_hours"] == 2.0 and bob["load_percent"] == 0.0 and bob["average_check"] == 0


async def test_status_trend(api, db, org_a, shop):
    db.add_all([
        _appt(org_a, shop, 0, shop.ann, datetime(2025, 1, 6, 10)),
        _appt(org_a, shop, 1, shop.ann, datetime(2025, 1, 6, 11), status="cancelled"),
        _appt(org_a, shop, 2, shop.ann, datetime(2025, 1, 14, 10), status="no_show"),
    ])
    await db.commit()
    params = {"date_from": "2025-01-06T00:00:00", "date_to": "2025-01-19T23:59:59", "group_by": "week"}
    r = await api.get(f"{org_a.base}{AN}/status-trend", headers=org_a.headers, params=params)
    assert r.status_code == 200, r.text
    points = r.json()["points"]
    assert [p["period_start"][:10] for p in points] == ["2025-01-06", "2025-01-13"]
    assert points[0] == {"period_start": "2025-01-06T00:00:00", "scheduled": 0, "completed": 1, "cancelled": 1,
                         "no_show": 0, "total": 2}
    assert points[1]["no_show"] == 1 and points[1]["total"] == 1
    long = {**params, "date_to": "2026-06-01T00:00:00"}
    assert (await api.get(f"{org_a.base}{AN}/status-trend", headers=org_a.headers, params=long)).status_code == 422
    days = await api.get(f"{org_a.base}{AN}/status-trend", headers=org_a.headers, params={**params, "group_by": "day"})
    assert len(days.json()["points"]) == 14
