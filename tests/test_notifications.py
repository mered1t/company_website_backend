"""Уведомления внутри CRM («колокольчик»): запись онлайн, отмена и перенос клиентом."""
from datetime import timedelta

import pytest
from sqlalchemy import func, select

import models
from core.rate_limiter import limiter
from core.time_utils import utc_now
from services.common import get_org_now
from services.notifications import delete_old_notifications
from services.reminders import reminder_loop
from tests.test_booking_emails import PUBLIC, _booked, _setup, _when
from tests.test_booking_manage import _book_body
from tests.test_org_data import _add_member

OWNER = "ownera@example.com"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _user_id(db, email):
    return (await db.execute(select(models.User.id).where(models.User.email == email))).scalar_one()


async def _rows(db, **filters):
    db.expire_all()
    query = select(models.Notification).order_by(models.Notification.id)
    for name, value in filters.items():
        query = query.where(getattr(models.Notification, name) == value)
    return (await db.execute(query)).scalars().all()


async def _member_with_master(db, org, role, master_id=None, username=None):
    user = models.User(username=username, email=f"{username}@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole(role),
                             master_id=master_id))
    await db.commit()
    return user.id


async def _notify(db, org, user_id, **fields):
    row = models.Notification(
        organization_id=org.org_id, user_id=user_id, type="booking_created", service_name="Haircut",
        master_name="Master Ann", start_time=utc_now(), **fields,
    )
    db.add(row)
    await db.commit()
    return row.id


# ------------------------------------------------------------------ события

async def test_online_booking_notifies_the_owner(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    r = await api.post(f"{PUBLIC}/{org_a.slug}/book", json=_book_body(service, master, start))
    assert r.status_code == 201, r.text

    rows = await _rows(db)
    assert len(rows) == 1
    n = rows[0]
    assert n.user_id == await _user_id(db, OWNER) and n.organization_id == org_a.org_id
    assert n.type == "booking_created" and n.appointment_id == r.json()["id"]
    assert (n.service_name, n.master_name, n.start_time, n.old_start_time) == ("Haircut", "Master Ann", start, None)
    assert n.read_at is None
    assert sent_emails == []  # салону письмо не уходит (клиент email не оставил)


async def test_cancel_by_the_client_notifies_and_does_not_email_the_salon(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)

    r = await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")
    assert r.status_code == 200, r.text

    rows = await _rows(db)
    assert [(x.type, x.appointment_id, x.start_time, x.old_start_time) for x in rows] == [
        ("booking_cancelled", booked.id, start, None)]
    assert [m["to"] for m in sent_emails] == ["bob@example.com"]  # письмо только клиенту


async def test_reschedule_by_the_client_keeps_both_times(api, db, org_a, sent_emails):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    new_start = start + timedelta(hours=2)
    booked = await _booked(db, org_a, service, master, start)

    r = await api.post(f"{PUBLIC}/booking/{booked.token}/reschedule", json={"start_time": new_start.isoformat()})
    assert r.status_code == 200, r.text

    rows = await _rows(db)
    assert [(x.type, x.start_time, x.old_start_time) for x in rows] == [("booking_rescheduled", new_start, start)]
    assert [m["to"] for m in sent_emails] == ["bob@example.com"]


async def test_second_cancel_click_does_not_repeat_it(api, db, org_a):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    for _ in range(2):
        assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 200
    assert len(await _rows(db)) == 1


async def test_too_late_cancel_and_failed_booking_create_nothing(api, db, org_a):
    service, master = await _setup(db, org_a)
    now = await get_org_now(db, org_a.org_id)
    booked = await _booked(db, org_a, service, master, now + timedelta(hours=1))
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 400
    r = await api.post(f"{PUBLIC}/{org_a.slug}/book", json=_book_body(service, master, now - timedelta(days=1)))
    assert r.status_code == 400
    assert await _rows(db) == []


async def test_staff_actions_create_no_notifications(api, db, org_a):
    service, master = await _setup(db, org_a)
    start = await _when(db, org_a)
    booked = await _booked(db, org_a, service, master, start)
    h = org_a.headers
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", json={"start_time": (start + timedelta(hours=1)).isoformat()}, headers=h)
    assert r.status_code == 200, r.text
    r = await api.patch(f"{org_a.base}/appointments/{booked.id}", json={"status": "cancelled"}, headers=h)
    assert r.status_code == 200, r.text
    assert await _rows(db) == []


async def test_closed_organization_gets_no_notifications(api, db, org_a):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    org = await db.get(models.Organization, org_a.org_id)
    org.paid_until = None
    org.trial_ends_at = utc_now() - timedelta(days=30)
    await db.commit()
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 200
    assert await _rows(db) == []


# ------------------------------------------------------------------ кому

async def test_managers_and_the_masters_account_get_it_but_other_masters_do_not(api, db, org_a, org_b):
    service, master = await _setup(db, org_a)
    other_master = models.Master(organization_id=org_a.org_id, full_name="Other")
    db.add(other_master)
    await db.flush()
    admin = await _member_with_master(db, org_a, "admin", username="adm")
    ann = await _member_with_master(db, org_a, "master", master.id, username="ann")
    other = await _member_with_master(db, org_a, "master", other_master.id, username="other")
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))

    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 200

    recipients = {x.user_id for x in await _rows(db)}
    assert recipients == {await _user_id(db, OWNER), admin, ann}
    assert other not in recipients
    assert await _user_id(db, "ownerb@example.com") not in recipients  # другая организация


# ------------------------------------------------------------------ API

async def test_list_is_mine_newest_first_with_client_name(api, db, org_a):
    service, master = await _setup(db, org_a)
    admin = await _member_with_master(db, org_a, "admin", username="adm")
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    me = await _user_id(db, OWNER)
    first = await _notify(db, org_a, me, appointment_id=booked.id, client_id=booked.client_id)
    second = await _notify(db, org_a, me)
    await _notify(db, org_a, admin)  # чужое уведомление

    r = await api.get(f"{org_a.base}/notifications", headers=org_a.headers)
    assert r.status_code == 200, r.text
    items = r.json()
    assert [i["id"] for i in items] == [second, first]
    assert items[1]["client_name"] == "Bob" and items[0]["client_name"] is None
    assert items[1]["appointment_id"] == booked.id and items[1]["is_read"] is False
    assert set(items[0]) == {"id", "type", "appointment_id", "client_name", "service_name", "master_name",
                             "start_time", "old_start_time", "created_at", "read_at", "is_read"}


async def test_client_name_follows_the_client_card(api, db, org_a):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    await _notify(db, org_a, await _user_id(db, OWNER), client_id=booked.client_id)
    client = await db.get(models.Client, booked.client_id)
    client.full_name = "[deleted]"  # так выглядит карточка после анонимизации
    await db.commit()
    r = await api.get(f"{org_a.base}/notifications", headers=org_a.headers)
    assert r.json()[0]["client_name"] == "[deleted]"


async def test_unread_filter_pagination_and_count(api, db, org_a):
    me = await _user_id(db, OWNER)
    ids = [await _notify(db, org_a, me) for _ in range(5)]
    h = org_a.headers
    assert (await api.get(f"{org_a.base}/notifications/unread-count", headers=h)).json() == {"unread": 5}

    assert (await api.post(f"{org_a.base}/notifications/{ids[4]}/read", headers=h)).status_code == 200
    assert (await api.get(f"{org_a.base}/notifications/unread-count", headers=h)).json() == {"unread": 4}

    unread = (await api.get(f"{org_a.base}/notifications?unread_only=true", headers=h)).json()
    assert [i["id"] for i in unread] == ids[3::-1]

    page = (await api.get(f"{org_a.base}/notifications?skip=1&limit=2", headers=h)).json()
    assert [i["id"] for i in page] == [ids[3], ids[2]]
    for bad in ("limit=0", "limit=101", "skip=-1"):
        assert (await api.get(f"{org_a.base}/notifications?{bad}", headers=h)).status_code == 422


async def test_read_one_sets_time_once_and_is_repeatable(api, db, org_a):
    nid = await _notify(db, org_a, await _user_id(db, OWNER))
    url = f"{org_a.base}/notifications/{nid}/read"
    first = await api.post(url, headers=org_a.headers)
    assert first.status_code == 200 and first.json()["is_read"] is True
    again = await api.post(url, headers=org_a.headers)
    assert again.status_code == 200 and again.json()["read_at"] == first.json()["read_at"]


async def test_read_all_touches_only_my_notifications_in_this_organization(api, db, org_a, org_b):
    me = await _user_id(db, OWNER)
    admin = await _member_with_master(db, org_a, "admin", username="adm")
    mine = [await _notify(db, org_a, me) for _ in range(3)]
    theirs = await _notify(db, org_a, admin)
    await _notify(db, org_b, await _user_id(db, "ownerb@example.com"))

    r = await api.post(f"{org_a.base}/notifications/read-all", headers=org_a.headers)
    assert r.status_code == 200 and r.json() == {"updated": 3}
    assert (await api.post(f"{org_a.base}/notifications/read-all", headers=org_a.headers)).json() == {"updated": 0}
    db.expire_all()
    assert [(await db.get(models.Notification, i)).read_at is not None for i in mine] == [True] * 3
    assert (await db.get(models.Notification, theirs)).read_at is None
    assert (await api.get(f"{org_a.base}/notifications/unread-count", headers=org_a.headers)).json() == {"unread": 0}


async def test_one_person_in_two_organizations_keeps_the_bells_apart(api, db, org_a, org_b):
    me = await _user_id(db, OWNER)
    db.add(models.Membership(user_id=me, organization_id=org_b.org_id, role=models.MembershipRole("admin")))
    await db.commit()
    in_a = [await _notify(db, org_a, me) for _ in range(2)]
    in_b = [await _notify(db, org_b, me) for _ in range(3)]
    h = org_a.headers

    listed = await api.get(f"{org_a.base}/notifications", headers=h)
    assert sorted(n["id"] for n in listed.json()) == sorted(in_a)
    assert (await api.get(f"{org_a.base}/notifications/unread-count", headers=h)).json() == {"unread": 2}
    assert (await api.post(f"{org_a.base}/notifications/{in_b[0]}/read", headers=h)).status_code == 404

    assert (await api.post(f"{org_a.base}/notifications/read-all", headers=h)).json() == {"updated": 2}
    db.expire_all()
    assert [(await db.get(models.Notification, i)).read_at is None for i in in_b] == [True] * 3
    assert (await api.get(f"{org_a.base}/notifications/unread-count", headers=h)).json() == {"unread": 0}


async def test_cannot_read_someone_elses_notification(api, db, org_a, org_b):
    admin = await _member_with_master(db, org_a, "admin", username="adm")
    foreign = await _notify(db, org_a, admin)  # другого сотрудника той же организации
    other_org = await _notify(db, org_b, await _user_id(db, "ownerb@example.com"))
    h = org_a.headers
    assert (await api.post(f"{org_a.base}/notifications/{foreign}/read", headers=h)).status_code == 404
    assert (await api.post(f"{org_a.base}/notifications/{other_org}/read", headers=h)).status_code == 404
    assert (await api.post(f"{org_a.base}/notifications/999999/read", headers=h)).status_code == 404
    db.expire_all()
    assert (await db.get(models.Notification, foreign)).read_at is None


async def test_other_organizations_notifications_are_not_reachable(api, db, org_a, org_b):
    await _notify(db, org_a, await _user_id(db, OWNER))
    r = await api.get(f"{org_a.base}/notifications", headers=org_b.headers)
    assert r.status_code in (403, 404)
    assert (await api.get(f"{org_a.base}/notifications")).status_code == 401


async def test_a_master_sees_only_own_notifications(api, db, org_a):
    service, master = await _setup(db, org_a)
    ann = await _member_with_master(db, org_a, "master", master.id, username="ann")
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    assert (await api.post(f"{PUBLIC}/booking/{booked.token}/cancel")).status_code == 200

    from auth.auth import create_access_token
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(ann)})}"}
    items = (await api.get(f"{org_a.base}/notifications", headers=headers)).json()
    assert len(items) == 1 and items[0]["type"] == "booking_cancelled"


async def test_closed_organization_answers_402_like_everything_else(api, db, org_a):
    org = await db.get(models.Organization, org_a.org_id)
    org.paid_until = None
    org.trial_ends_at = utc_now() - timedelta(days=30)
    await db.commit()
    assert (await api.get(f"{org_a.base}/notifications", headers=org_a.headers)).status_code == 402


async def test_notifications_exist_only_in_v1(api, org_a):
    r = await api.get(f"/api/organizations/{org_a.org_id}/notifications", headers=org_a.headers)
    assert r.status_code == 404


# ------------------------------------------------------------------ хранение и удаление

async def test_old_notifications_are_removed_after_30_days(db, org_a):
    me = await _user_id(db, OWNER)
    old = await _notify(db, org_a, me)
    edge = await _notify(db, org_a, me)
    fresh = await _notify(db, org_a, me)
    for nid, age in ((old, timedelta(days=30, minutes=1)), (edge, timedelta(days=29, hours=23))):
        row = await db.get(models.Notification, nid)
        row.created_at = utc_now() - age
    await db.commit()

    assert await delete_old_notifications(db) == 1
    assert {n.id for n in await _rows(db)} == {edge, fresh}


async def test_background_loop_cleans_up(db, org_a, session_factory_):
    old = await _notify(db, org_a, await _user_id(db, OWNER))
    row = await db.get(models.Notification, old)
    row.created_at = utc_now() - timedelta(days=40)
    await db.commit()

    import asyncio
    task = asyncio.create_task(reminder_loop(session_factory_, interval_seconds=3600, initial_delay=0))
    for _ in range(50):
        if not await _rows(db):
            break
        await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _rows(db) == []


@pytest.fixture
def session_factory_(db):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
    return async_sessionmaker(db.bind, class_=AsyncSession, expire_on_commit=False)


async def test_deleting_the_organization_removes_its_notifications(db, org_a, org_b):
    from services.org_data import delete_organization_data
    await _notify(db, org_a, await _user_id(db, OWNER))
    keep = await _notify(db, org_b, await _user_id(db, "ownerb@example.com"))
    await delete_organization_data(db, org_a.org_id)
    await db.commit()
    assert [n.id for n in await _rows(db)] == [keep]


async def test_deleting_an_account_removes_its_notifications(db, org_a):
    from services.account import delete_user_account
    admin = await _member_with_master(db, org_a, "admin", username="adm")
    await _notify(db, org_a, admin)
    keep = await _notify(db, org_a, await _user_id(db, OWNER))
    await delete_user_account(db, await db.get(models.User, admin))
    await db.commit()
    assert [n.id for n in await _rows(db)] == [keep]


async def test_deleting_a_client_or_appointment_keeps_the_notification(db, org_a):
    service, master = await _setup(db, org_a)
    booked = await _booked(db, org_a, service, master, await _when(db, org_a))
    nid = await _notify(db, org_a, await _user_id(db, OWNER), appointment_id=booked.id, client_id=booked.client_id)
    from sqlalchemy import delete as sa_delete
    await db.execute(sa_delete(models.AppointmentToken))
    await db.execute(sa_delete(models.Appointment).where(models.Appointment.id == booked.id))
    await db.execute(sa_delete(models.Client).where(models.Client.id == booked.client_id))
    await db.commit()
    row = await db.get(models.Notification, nid)
    assert row.appointment_id is None and row.client_id is None
