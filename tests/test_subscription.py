"""Подписка: статусы по датам, блокировка доступа (402), публичная запись, ИИ на триале, напоминания."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import models
from core.time_utils import utc_now
from domain.subscription import GRACE_DAYS, TRIAL_DAYS, SubscriptionStatus, add_months, get_subscription
from services.billing import access_filter
from services.booking_tokens import create_token
from services.common import to_org_local
from services.reminders import send_due_reminders
from tests.test_org_data import PASSWORD, _add_member

NOW = datetime(2030, 1, 15, 12, 0)
DAY = timedelta(days=1)
S = SubscriptionStatus


def org_like(**overrides):
    values = dict(plan="basic", trial_ends_at=NOW - 100 * DAY, paid_until=None, is_free=False, is_blocked=False)
    values.update(overrides)
    return SimpleNamespace(**values)


# ------------------------------------------------------------------ чистая логика статусов

CASES = [
    # название, поля организации, статус, доступ, ИИ, дней до границы
    ("trial", dict(trial_ends_at=NOW + 10 * DAY), S.trial, True, True, 10),
    ("trial last hour", dict(trial_ends_at=NOW + timedelta(hours=1)), S.trial, True, True, 1),
    ("trial just ended", dict(trial_ends_at=NOW - timedelta(hours=1)), S.grace, True, False, 7),
    ("grace last day", dict(trial_ends_at=NOW - 6 * DAY - timedelta(hours=1)), S.grace, True, False, 1),
    ("grace boundary", dict(trial_ends_at=NOW - GRACE_DAYS * DAY), S.grace, True, False, 0),
    ("grace over", dict(trial_ends_at=NOW - GRACE_DAYS * DAY - timedelta(seconds=1)), S.expired, False, False, 0),
    ("paid basic", dict(paid_until=NOW + 20 * DAY), S.active, True, False, 20),
    ("paid pro", dict(paid_until=NOW + 20 * DAY, plan="pro"), S.active, True, True, 20),
    ("paid expired, in grace", dict(paid_until=NOW - 3 * DAY), S.grace, True, False, 4),
    ("paid expired, long ago", dict(paid_until=NOW - 10 * DAY), S.expired, False, False, 0),
    ("paid expired but trial running", dict(paid_until=NOW - 3 * DAY, trial_ends_at=NOW + 2 * DAY), S.trial, True, True, 2),
    ("free basic", dict(is_free=True), S.free, True, False, None),
    ("free pro", dict(is_free=True, plan="pro"), S.free, True, True, None),
    ("blocked", dict(is_blocked=True, paid_until=NOW + 20 * DAY), S.blocked, False, False, None),
    ("blocked beats free", dict(is_blocked=True, is_free=True, plan="pro"), S.blocked, False, False, None),
    ("no dates at all", dict(trial_ends_at=None), S.expired, False, False, 0),
]


@pytest.mark.parametrize("name,fields,status,access,ai,days_left", CASES, ids=[c[0] for c in CASES])
def test_status_from_dates(name, fields, status, access, ai, days_left):
    sub = get_subscription(org_like(**fields), NOW)
    assert sub.status == status
    assert sub.has_access is access
    assert sub.booking_enabled is access
    assert sub.ai_enabled is ai
    assert sub.days_left == days_left


def test_trial_gives_all_features_but_the_plan_stays_basic():
    sub = get_subscription(org_like(trial_ends_at=NOW + DAY), NOW)
    assert sub.plan == "basic" and sub.effective_plan == "pro" and sub.ai_enabled


def test_grace_end_is_shown_only_while_access_lasts():
    assert get_subscription(org_like(trial_ends_at=NOW + DAY), NOW).grace_ends_at == NOW + DAY + GRACE_DAYS * DAY
    assert get_subscription(org_like(is_free=True), NOW).grace_ends_at is None
    assert get_subscription(org_like(), NOW).grace_ends_at is None  # давно истёк


@pytest.mark.parametrize("start,months,expected", [
    (datetime(2030, 1, 31, 10, 30), 1, datetime(2030, 2, 28, 10, 30)),
    (datetime(2032, 1, 31), 1, datetime(2032, 2, 29)),
    (datetime(2030, 12, 15), 1, datetime(2031, 1, 15)),
    (datetime(2030, 3, 31), 12, datetime(2031, 3, 31)),
    (datetime(2030, 11, 30), 3, datetime(2031, 2, 28)),
])
def test_add_months(start, months, expected):
    assert add_months(start, months) == expected


# ------------------------------------------------------------------ SQL-условие совпадает с Python

async def test_sql_access_filter_matches_python(db):
    orgs = []
    for i, (name, fields, *_rest) in enumerate(c for c in CASES if c[1].get("trial_ends_at", 1) is not None):
        values = dict(trial_ends_at=NOW - 100 * DAY)
        values.update(fields)
        orgs.append(models.Organization(name=name, slug=f"case-{i}", **values))
    db.add_all(orgs)
    await db.commit()

    allowed = set((await db.execute(select(models.Organization.id).where(access_filter(NOW)))).scalars().all())
    for org in orgs:
        assert (org.id in allowed) == get_subscription(org, NOW).has_access, org.name


# ------------------------------------------------------------------ доступ через API

async def _set(db, org_id, **fields):
    org = (await db.execute(
        select(models.Organization).where(models.Organization.id == org_id).execution_options(populate_existing=True),
    )).scalar_one()
    for key, value in fields.items():
        setattr(org, key, value)
    await db.commit()


def _expired():
    return dict(paid_until=None, trial_ends_at=utc_now() - (GRACE_DAYS + 1) * DAY)


def _grace():
    return dict(paid_until=None, trial_ends_at=utc_now() - DAY)


def _trial():
    return dict(paid_until=None, trial_ends_at=utc_now() + 5 * DAY)


async def test_new_organization_starts_a_trial_with_ai(api, org_a):
    r = await api.post("/api/v1/organizations", json={"name": "Fresh shop"}, headers=org_a.headers)
    assert r.status_code == 201, r.text
    fresh_id = r.json()["id"]

    mine = {o["id"]: o for o in (await api.get("/api/v1/organizations", headers=org_a.headers)).json()}
    sub = mine[fresh_id]["subscription"]
    assert sub["status"] == "trial"
    assert sub["days_left"] == TRIAL_DAYS
    assert sub["plan"] == "basic" and sub["ai_enabled"] is True
    assert sub["has_access"] and sub["booking_enabled"]

    usage = await api.get(f"/api/v1/organizations/{fresh_id}/analytics/ai-usage", headers=org_a.headers)
    assert usage.status_code == 200 and usage.json()["ai_enabled"] is True


async def test_organizations_used_by_other_tests_are_paid_basic(api, org_a):
    (org,) = (await api.get("/api/v1/organizations", headers=org_a.headers)).json()
    assert org["subscription"]["status"] == "active"
    assert org["subscription"]["plan"] == "basic" and org["subscription"]["ai_enabled"] is False


@pytest.mark.parametrize("state,expected", [
    (_trial, 200), (_grace, 200), (_expired, 402), (lambda: dict(is_blocked=True), 403),
    (lambda: dict(is_free=True, **_expired()), 200),
])
async def test_cabinet_access_follows_the_subscription(api, db, org_a, state, expected):
    await _set(db, org_a.org_id, **state())
    r = await api.get(f"{org_a.base}/appointments", headers=org_a.headers)
    assert r.status_code == expected, r.text
    if expected == 402:
        assert "Subscription expired" in r.json()["detail"]


async def test_all_roles_are_blocked_when_expired(api, db, org_a):
    admin_headers, _ = await _add_member(db, org_a, "admin")
    master_headers, _ = await _add_member(db, org_a, "master")
    await _set(db, org_a.org_id, **_expired())
    for headers in (org_a.headers, admin_headers, master_headers):
        assert (await api.get(f"{org_a.base}/appointments", headers=headers)).status_code == 402
        assert (await api.get(f"{org_a.base}/members", headers=headers)).status_code == 402


async def test_access_comes_back_after_payment_date_is_set(api, db, org_a):
    await _set(db, org_a.org_id, **_expired())
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 402
    await _set(db, org_a.org_id, paid_until=utc_now() + 30 * DAY)
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 200


@pytest.mark.parametrize("state", [_expired, lambda: dict(is_blocked=True)])
async def test_subscription_info_export_and_list_work_even_when_closed(api, db, org_a, state):
    await _set(db, org_a.org_id, **state())

    mine = await api.get("/api/v1/organizations", headers=org_a.headers)
    assert mine.status_code == 200 and mine.json()[0]["subscription"]["has_access"] is False

    r = await api.get(f"{org_a.base}/subscription", headers=org_a.headers)
    assert r.status_code == 200 and r.json()["has_access"] is False

    assert (await api.get(f"{org_a.base}/export", headers=org_a.headers)).status_code == 200
    me = await api.get("/api/v1/users/me", headers=org_a.headers)
    assert me.status_code == 200


@pytest.mark.parametrize("state", [_expired, lambda: dict(is_blocked=True)])
async def test_owner_can_still_delete_a_closed_organization(api, db, org_a, state):
    await _set(db, org_a.org_id, **state())
    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers,
                       json={"password": PASSWORD, "confirm_name": "Shop ownera"})
    assert r.status_code == 204, r.text


async def test_subscription_endpoint_is_for_members_only(api, org_a, org_b):
    assert (await api.get(f"{org_a.base}/subscription", headers=org_b.headers)).status_code == 403
    assert (await api.get(f"{org_a.base}/subscription")).status_code == 401


async def test_other_organizations_are_not_affected(api, db, org_a, org_b):
    await _set(db, org_a.org_id, **_expired())
    assert (await api.get(f"{org_b.base}/appointments", headers=org_b.headers)).status_code == 200


# ------------------------------------------------------------------ ИИ

async def test_ai_follows_trial_and_plan(api, db, org_a):
    url = f"{org_a.base}/analytics/ai-usage"
    cases = [
        (dict(paid_until=utc_now() + 30 * DAY, plan="basic"), 200, False),
        (dict(paid_until=utc_now() + 30 * DAY, plan="pro"), 200, True),
        (dict(paid_until=None, plan="basic", trial_ends_at=utc_now() + 5 * DAY), 200, True),  # триал
        (dict(plan="basic", trial_ends_at=utc_now() - DAY), 200, False),  # льготный период: триал закончился
        (dict(plan="pro", **_expired()), 402, None),
    ]
    for fields, code, ai in cases:
        await _set(db, org_a.org_id, **fields)
        r = await api.get(url, headers=org_a.headers)
        assert r.status_code == code, (fields, r.text)
        if ai is not None:
            assert r.json()["ai_enabled"] is ai, fields


# ------------------------------------------------------------------ публичная запись

@pytest.mark.parametrize("state,enabled", [
    (_trial, True), (_grace, True), (_expired, False), (lambda: dict(is_blocked=True), False),
])
async def test_public_booking_follows_the_subscription(api, db, org_a, state, enabled):
    await _set(db, org_a.org_id, **state())
    base = f"/api/v1/public/{org_a.slug}"

    info = await api.get(base)  # страница записи всегда отвечает, чтобы показать «запись недоступна»
    assert info.status_code == 200
    assert info.json()["booking_enabled"] is enabled

    for path in ("/services", "/masters"):
        r = await api.get(base + path)
        assert r.status_code == (200 if enabled else 403), (path, r.text)
    r = await api.get(base + "/available-slots", params={"master_id": 1, "service_id": 1, "date": "2030-01-01"})
    assert (r.status_code != 403) == enabled, r.text
    r = await api.get(base + "/available-dates", params={"master_id": 1, "service_id": 1, "month": "2030-01"})
    assert (r.status_code != 403) == enabled, r.text
    r = await api.post(base + "/book", json={
        "client_full_name": "Anna", "client_phone": "+380991112233", "master_id": 1, "service_id": 1,
        "start_time": "2030-01-01T10:00:00",
    })
    assert (r.status_code != 403) == enabled, r.text


async def _make_appointment(db, org_id, tag, *, hours_ahead, token_age_days=0, email=None):
    """Запись с секретной ссылкой. Возвращает (id записи, токен из письма)."""
    service = models.Service(organization_id=org_id, name=f"{tag}-service", price=1500, duration_minutes=45)
    master = models.Master(organization_id=org_id, full_name=f"{tag}-master")
    client = models.Client(organization_id=org_id, full_name=f"{tag}-client", phone="+380671111111",
                           email=email or f"{tag}@example.com")
    db.add_all([service, master, client])
    await db.flush()
    start = to_org_local(utc_now(), "Europe/Kiev").replace(microsecond=0) + timedelta(hours=hours_ahead)
    appointment = models.Appointment(
        organization_id=org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(minutes=45), status="scheduled", price=1500, currency="EUR",
    )
    db.add(appointment)
    await db.flush()
    token = await create_token(db, appointment.id, "en", client.email)
    if token_age_days:
        row = (await db.execute(select(models.AppointmentToken).where(
            models.AppointmentToken.appointment_id == appointment.id))).scalar_one()
        row.created_at = utc_now() - timedelta(days=token_age_days)
    await db.commit()
    return appointment.id, token


async def test_client_can_view_and_cancel_but_not_reschedule_when_the_salon_is_closed(api, db, org_a):
    _, token = await _make_appointment(db, org_a.org_id, "A", hours_ahead=72)
    url = f"/api/v1/public/booking/{token}"
    day = (utc_now() + 2 * DAY).date().isoformat()

    assert (await api.get(f"{url}/available-slots", params={"date": day})).status_code == 200  # пока всё работает

    await _set(db, org_a.org_id, **_expired())
    assert (await api.get(url)).status_code == 200
    assert (await api.get(f"{url}/available-slots", params={"date": day})).status_code == 403
    r = await api.post(f"{url}/reschedule", json={"start_time": (utc_now() + 3 * DAY).isoformat()})
    assert r.status_code == 403

    r = await api.post(f"{url}/cancel")  # отмена всегда доступна: салону она тоже на руку
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


# ------------------------------------------------------------------ напоминания

async def test_no_reminders_for_closed_organizations(db, org_a, org_b, sent_emails):
    a_id, _ = await _make_appointment(db, org_a.org_id, "A", hours_ahead=3, token_age_days=3)
    b_id, _ = await _make_appointment(db, org_b.org_id, "B", hours_ahead=3, token_age_days=3)
    await _set(db, org_a.org_id, **_expired())

    assert await send_due_reminders(db) == 1
    assert len(sent_emails) == 1

    reminded = {a.id: a.reminded_start_time for a in (await db.execute(
        select(models.Appointment).execution_options(populate_existing=True))).scalars().all()}
    assert reminded[a_id] is None
    assert reminded[b_id] is not None


async def test_reminders_continue_in_the_grace_period(db, org_a, sent_emails):
    await _make_appointment(db, org_a.org_id, "A", hours_ahead=3, token_age_days=3)
    await _set(db, org_a.org_id, **_grace())
    assert await send_due_reminders(db) == 1
