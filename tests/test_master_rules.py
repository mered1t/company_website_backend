"""Права мастера: видит только свои записи; закрытую запись правит только в день визита;
«выполнена» ставит только начавшейся записи. Владелец и админ могут всё."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

import models
from auth.auth import create_access_token
from services.common import get_org_now

CLOSED_LATE = "Only the owner or an admin can change a closed appointment after its day"
TOO_EARLY = "An appointment can be marked completed only after it has started"


@pytest.fixture
async def shop(db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Haircut", price=1000, duration_minutes=60)
    service_b = models.Service(organization_id=org_a.org_id, name="Beard", price=500, duration_minutes=60)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")
    client = models.Client(organization_id=org_a.org_id, full_name="Client One", phone="+380670000111")
    db.add_all([service, service_b, ann, bob, client])
    await db.flush()
    user = models.User(username="ann-user", email="ann-user@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org_a.org_id, role=models.MembershipRole.master,
                             master_id=ann.id))
    await db.commit()
    return SimpleNamespace(
        service=service.id, service_b=service_b.id, ann=ann.id, bob=bob.id, client=client.id,
        ann_headers={"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"},
    )


async def _appointment(db, org, shop, master_id, start, status="scheduled"):
    row = models.Appointment(
        organization_id=org.org_id, client_id=shop.client, service_id=shop.service, master_id=master_id,
        start_time=start, end_time=start + timedelta(hours=1), status=status, price=1000, currency="EUR",
    )
    db.add(row)
    await db.commit()
    return row.id


async def _now(db, org):
    return (await get_org_now(db, org.org_id)).replace(second=0, microsecond=0)


def _url(org, appointment_id=""):
    return f"{org.base}/appointments" + (f"/{appointment_id}" if appointment_id != "" else "")


# ------------------------------------------------------------------ что видит мастер
async def test_master_sees_only_own_appointments(api, db, org_a, shop):
    now = await _now(db, org_a)
    mine = await _appointment(db, org_a, shop, shop.ann, now + timedelta(days=1))
    theirs = await _appointment(db, org_a, shop, shop.bob, now + timedelta(days=1, hours=2))

    listed = await api.get(_url(org_a), headers=shop.ann_headers)
    assert [a["id"] for a in listed.json()] == [mine]
    params = {"date_from": (now - timedelta(days=1)).isoformat(), "date_to": (now + timedelta(days=3)).isoformat()}
    calendar = await api.get(f"{_url(org_a)}/calendar", headers=shop.ann_headers, params=params)
    assert [a["id"] for a in calendar.json()] == [mine]
    asked_for_bob = await api.get(f"{_url(org_a)}/calendar", headers=shop.ann_headers,
                                  params={**params, "master_id": shop.bob})
    assert asked_for_bob.json() == []
    assert (await api.get(_url(org_a, theirs), headers=shop.ann_headers)).status_code == 404
    assert (await api.get(_url(org_a, mine), headers=shop.ann_headers)).status_code == 200

    owner_calendar = await api.get(f"{_url(org_a)}/calendar", headers=org_a.headers, params=params)
    assert sorted(a["id"] for a in owner_calendar.json()) == sorted([mine, theirs])


async def test_master_sees_all_clients_but_only_own_visits_in_their_history(api, db, org_a, shop):
    now = await _now(db, org_a)
    mine = await _appointment(db, org_a, shop, shop.ann, now - timedelta(days=3), status="completed")
    theirs = await _appointment(db, org_a, shop, shop.bob, now - timedelta(days=2), status="completed")

    clients = await api.get(f"{org_a.base}/clients", headers=shop.ann_headers)
    assert shop.client in [c["id"] for c in clients.json()]
    history = await api.get(f"{org_a.base}/clients/{shop.client}/appointments", headers=shop.ann_headers)
    assert [a["id"] for a in history.json()] == [mine]
    owner_history = await api.get(f"{org_a.base}/clients/{shop.client}/appointments", headers=org_a.headers)
    assert sorted(a["id"] for a in owner_history.json()) == sorted([mine, theirs])


async def test_calendar_accepts_dates_with_a_timezone(api, db, org_a, shop):
    r = await api.get(f"{_url(org_a)}/calendar", headers=org_a.headers,
                      params={"date_from": "2030-01-01T00:00:00+02:00", "date_to": "2030-01-31T23:59:00+02:00"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ «выполнена»
async def test_master_marks_completed_only_after_the_start(api, db, org_a, shop):
    now = await _now(db, org_a)
    future = await _appointment(db, org_a, shop, shop.ann, now + timedelta(hours=3))
    started = await _appointment(db, org_a, shop, shop.ann, now - timedelta(minutes=30))

    r = await api.patch(_url(org_a, future), headers=shop.ann_headers, json={"status": "completed"})
    assert r.status_code == 400 and r.json()["detail"] == TOO_EARLY
    r = await api.patch(_url(org_a, started), headers=shop.ann_headers, json={"status": "completed"})
    assert r.status_code == 200, r.text


async def test_owner_can_close_the_day_in_advance(api, db, org_a, shop):
    now = await _now(db, org_a)
    future = await _appointment(db, org_a, shop, shop.ann, now + timedelta(hours=3))
    r = await api.patch(_url(org_a, future), headers=org_a.headers, json={"status": "completed"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ закрытая запись
async def test_master_can_fix_a_closed_appointment_on_the_same_day(api, db, org_a, shop):
    now = await _now(db, org_a)
    start = now - timedelta(minutes=5)
    if start.date() != now.date():
        pytest.skip("тест запущен в первые минуты суток")
    appointment_id = await _appointment(db, org_a, shop, shop.ann, start, status="completed")
    r = await api.patch(_url(org_a, appointment_id), headers=shop.ann_headers, json={"status": "no_show"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("body", [
    {"status": "cancelled"},
    {"status": "scheduled"},
    {"service_id": "SERVICE_B"},
    {"start_time": "MOVE"},
])
async def test_master_cannot_rewrite_a_closed_appointment_later(api, db, org_a, shop, body):
    now = await _now(db, org_a)
    start = (now - timedelta(days=1)).replace(hour=10, minute=0)
    appointment_id = await _appointment(db, org_a, shop, shop.ann, start, status="completed")
    if body.get("service_id") == "SERVICE_B":
        body = {"service_id": shop.service_b}
    if body.get("start_time") == "MOVE":
        body = {"start_time": (start + timedelta(hours=1)).isoformat()}

    r = await api.patch(_url(org_a, appointment_id), headers=shop.ann_headers, json=body)
    assert r.status_code == 403 and r.json()["detail"] == CLOSED_LATE
    db.expire_all()
    row = await db.get(models.Appointment, appointment_id)
    assert (row.status, row.service_id, row.price, row.start_time) == ("completed", shop.service, 1000, start)


async def test_master_can_still_add_notes_to_an_old_visit(api, db, org_a, shop):
    now = await _now(db, org_a)
    appointment_id = await _appointment(db, org_a, shop, shop.ann, now - timedelta(days=2), status="completed")
    r = await api.patch(_url(org_a, appointment_id), headers=shop.ann_headers, json={"notes": "Used new color"})
    assert r.status_code == 200, r.text


async def test_master_can_close_a_forgotten_past_appointment(api, db, org_a, shop):
    """Вчерашнюю запись мастер забыл закрыть: она ещё «запланирована», закрыть её можно."""
    now = await _now(db, org_a)
    appointment_id = await _appointment(db, org_a, shop, shop.ann, now - timedelta(days=1))
    r = await api.patch(_url(org_a, appointment_id), headers=shop.ann_headers, json={"status": "completed"})
    assert r.status_code == 200, r.text


@pytest.mark.parametrize("role", ["owner", "admin"])
async def test_owner_and_admin_can_correct_old_visits(api, db, org_a, shop, role):
    from tests.test_org_data import _add_member
    headers = org_a.headers if role == "owner" else (await _add_member(db, org_a, "admin"))[0]
    now = await _now(db, org_a)
    appointment_id = await _appointment(db, org_a, shop, shop.ann, now - timedelta(days=5), status="completed")
    r = await api.patch(_url(org_a, appointment_id), headers=headers, json={"status": "cancelled"})
    assert r.status_code == 200, r.text
