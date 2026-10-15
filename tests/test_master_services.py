"""Услуги мастера: удалённые услуги не видны и не мешают записи; список услуг можно поменять (PUT .../services)."""
from datetime import timedelta

import pytest

from core.rate_limiter import limiter
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE
from tests.test_org_data import _add_member
from tests.test_tenant_isolation import make_service


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _master(api, org, service_ids):
    r = await api.post(f"{org.base}/masters", headers=org.headers, json={
        "full_name": "Svc Master", "service_ids": service_ids,
        "working_hours": [{"day_of_week": d, "start_time": "09:00", "end_time": "19:00"} for d in range(7)],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _service_ids(api, org, master_id):
    r = await api.get(f"{org.base}/masters/{master_id}", headers=org.headers)
    assert r.status_code == 200, r.text
    return sorted(s["id"] for s in r.json()["services"])


async def _put(api, org, master_id, ids, headers=None):
    return await api.put(f"{org.base}/masters/{master_id}/services", headers=headers or org.headers,
                         json={"service_ids": ids})


# ------------------------------------------------------------------ удалённые услуги
async def test_deleted_service_disappears_from_the_master_and_the_public_page(api, db, org_a):
    cut, color = await make_service(api, org_a, "Cut"), await make_service(api, org_a, "Color")
    master = await _master(api, org_a, [cut, color])
    assert (await api.delete(f"{org_a.base}/services/{color}", headers=org_a.headers)).status_code == 204

    assert await _service_ids(api, org_a, master) == [cut]
    public = (await api.get(f"{SLUG_BASE}/{org_a.slug}/masters")).json()
    assert [s["id"] for m in public if m["id"] == master for s in m["services"]] == [cut]

    # услуга вернулась — вернулась и у мастера
    assert (await api.post(f"{org_a.base}/services/{color}/restore", headers=org_a.headers)).status_code == 200
    assert await _service_ids(api, org_a, master) == sorted([cut, color])


async def test_master_whose_only_service_was_deleted_can_still_be_booked(api, db, org_a):
    only, other = await make_service(api, org_a, "Only"), await make_service(api, org_a, "Other")
    master = await _master(api, org_a, [only])
    assert (await api.delete(f"{org_a.base}/services/{only}", headers=org_a.headers)).status_code == 204

    start = ((await get_org_now(db, org_a.org_id)) + timedelta(days=2)).replace(hour=11, minute=0, second=0,
                                                                                 microsecond=0)
    r = await api.post(f"{SLUG_BASE}/{org_a.slug}/book", json={
        "client_full_name": "Guest", "client_phone": "+380671112299", "master_id": master,
        "service_id": other, "start_time": start.isoformat()})
    assert r.status_code == 201, r.text


# ------------------------------------------------------------------ PUT /masters/{id}/services
async def test_replace_the_services_of_a_master(api, db, org_a):
    a = await make_service(api, org_a, "A")
    b = await make_service(api, org_a, "B")
    c = await make_service(api, org_a, "C")
    master = await _master(api, org_a, [a])

    r = await _put(api, org_a, master, [b, c, b])
    assert r.status_code == 200, r.text
    assert sorted(s["id"] for s in r.json()["services"]) == sorted([b, c])
    assert await _service_ids(api, org_a, master) == sorted([b, c])

    assert (await _put(api, org_a, master, [])).json()["services"] == []


async def test_services_must_be_active_services_of_this_salon(api, db, org_a, org_b):
    mine = await make_service(api, org_a, "Mine")
    gone = await make_service(api, org_a, "Gone")
    foreign = await make_service(api, org_b, "Foreign")
    master = await _master(api, org_a, [mine])
    assert (await api.delete(f"{org_a.base}/services/{gone}", headers=org_a.headers)).status_code == 204

    for bad in ([foreign], [gone], [mine, 999999]):
        r = await _put(api, org_a, master, bad)
        assert r.status_code == 400 and r.json()["detail"].startswith("Unknown services"), r.text
    assert await _service_ids(api, org_a, master) == [mine]  # ничего не поменялось


async def test_only_managers_of_this_salon_can_change_services(api, db, org_a, org_b):
    s = await make_service(api, org_a, "S")
    master = await _master(api, org_a, [s])
    master_headers, _ = await _add_member(db, org_a, "master")
    assert (await _put(api, org_a, master, [], headers=master_headers)).status_code == 403
    admin_headers, _ = await _add_member(db, org_a, "admin")
    assert (await _put(api, org_a, master, [], headers=admin_headers)).status_code == 200
    r = await api.put(f"{org_b.base}/masters/{master}/services", headers=org_b.headers, json={"service_ids": []})
    assert r.status_code == 404
    assert (await _put(api, org_a, 999999, [])).status_code == 404
