"""Тип бизнеса при создании салона и переключатель групповых записей (несколько клиентов в одной записи)."""
from datetime import datetime, timedelta

import pytest

import models
from core.rate_limiter import limiter
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE

OFF = "Group bookings are turned off"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _new_org(api, headers, **body):
    r = await api.post("/api/v1/organizations", headers=headers, json={"name": "Second shop", **body})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.mark.parametrize("business_type, expected", [("sports", True), ("studio", True), ("beauty", False),
                                                     ("other", False), (None, False)])
async def test_business_type_sets_the_default(api, org_a, business_type, expected):
    body = {"business_type": business_type} if business_type else {}
    org = await _new_org(api, org_a.headers, **body)
    assert org["business_type"] == business_type and org["group_bookings"] is expected


async def test_explicit_switch_wins_and_can_be_changed(api, org_a):
    org = await _new_org(api, org_a.headers, business_type="beauty", group_bookings=True)
    assert org["group_bookings"] is True
    assert (await _new_org(api, org_a.headers, business_type="sports", group_bookings=False))["group_bookings"] is False
    r = await api.post("/api/v1/organizations", headers=org_a.headers, json={"name": "X", "business_type": "bank"})
    assert r.status_code == 422

    url = f"{org_a.base}"
    r = await api.patch(url, headers=org_a.headers, json={"group_bookings": False, "business_type": "beauty"})
    assert r.status_code == 200 and r.json()["group_bookings"] is False and r.json()["business_type"] == "beauty"
    assert (await api.patch(url, headers=org_a.headers, json={"group_bookings": None})).status_code == 422
    r = await api.patch(url, headers=org_a.headers, json={"business_type": None})
    assert r.status_code == 200 and r.json()["business_type"] is None


async def test_everything_group_is_refused_when_off(api, db, org_a):
    service = models.Service(organization_id=org_a.org_id, name="Tennis", price=1000, duration_minutes=60,
                             max_party_size=4)
    master = models.Master(organization_id=org_a.org_id, full_name="Coach")
    client = models.Client(organization_id=org_a.org_id, full_name="Host", phone="+380670000501")
    friend = models.Client(organization_id=org_a.org_id, full_name="Friend", phone="+380670000502")
    db.add_all([service, master, client, friend])
    await db.flush()
    db.add_all([models.WorkingHours(master_id=master.id, day_of_week=d, start_time="08:00", end_time="20:00")
                for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=2)
    start = datetime.combine(day, datetime.min.time()).replace(hour=10).isoformat()
    ids = dict(service_id=service.id, master_id=master.id)

    # пока включено, в запись можно добавить ещё клиента
    group = await api.post(f"{org_a.base}/appointments", headers=org_a.headers, json={
        "client_id": client.id, **ids, "start_time": start, "participant_ids": [friend.id]})
    assert group.status_code == 201 and group.json()["party_size"] == 2

    await api.patch(org_a.base, headers=org_a.headers, json={"group_bookings": False})
    info = (await api.get(f"{SLUG_BASE}/{org_a.slug}")).json()
    assert info["group_bookings"] is False

    later = datetime.combine(day, datetime.min.time()).replace(hour=12).isoformat()
    checks = [
        await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "Duo", "price": 100, "duration_minutes": 30, "max_party_size": 2}),
        await api.patch(f"{org_a.base}/services/{service.id}", headers=org_a.headers, json={"max_party_size": 3}),
        await api.post(f"{org_a.base}/appointments", headers=org_a.headers,
                       json={"client_id": client.id, **ids, "start_time": later, "participant_ids": [friend.id]}),
    ]
    for r in checks:
        assert r.status_code == 400 and OFF in r.json()["detail"], r.text

    # одиночная запись работает как обычно, а из старой групповой можно убрать остальных
    r = await api.post(f"{org_a.base}/appointments", headers=org_a.headers,
                       json={"client_id": client.id, **ids, "start_time": later})
    assert r.status_code == 201, r.text
    r = await api.patch(f"{org_a.base}/appointments/{group.json()['id']}", headers=org_a.headers,
                        json={"participant_ids": []})
    assert r.status_code == 200 and r.json()["party_size"] == 1 and r.json()["price"] == 1000
