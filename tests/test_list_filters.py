"""Серверные сортировка и фильтры: клиенты (поиск, sort/dir, X-Total-Count),
записи и календарь (несколько мастеров/статусов/услуг сразу, X-Total-Count, мастер видит только свои)."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from auth.auth import create_access_token

TOTAL = "x-total-count"
DAY = datetime(2025, 3, 3, 10)


@pytest.fixture
async def shop(db, org_a):
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    beard = models.Service(organization_id=org_a.org_id, name="Beard", price=500, duration_minutes=60)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")
    eve = models.Master(organization_id=org_a.org_id, full_name="Eve")
    clients = [
        models.Client(organization_id=org_a.org_id, full_name="boris", phone="+380670000001",
                      created_at=datetime(2025, 1, 2)),
        models.Client(organization_id=org_a.org_id, full_name="Anna", phone="+380670000002",
                      created_at=datetime(2025, 1, 3)),
        models.Client(organization_id=org_a.org_id, full_name="Clara 50%_off", phone="+380990000003",
                      created_at=datetime(2025, 1, 1)),
    ]
    db.add_all([cut, beard, ann, bob, eve, *clients])
    await db.flush()
    user = models.User(username="ann-filters", email="ann-filters@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org_a.org_id, role=models.MembershipRole.master,
                             master_id=ann.id))
    rows = [  # (мастер, услуга, клиент, сдвиг в часах, статус)
        (ann, cut, clients[0], 0, "completed"),
        (bob, beard, clients[0], 1, "scheduled"),
        (eve, cut, clients[1], 2, "cancelled"),
        (ann, beard, clients[1], 26, "no_show"),
        (bob, cut, clients[1], 27, "completed"),
    ]
    ids = []
    for master, service, client, hours, status in rows:
        a = models.Appointment(organization_id=org_a.org_id, client_id=client.id, service_id=service.id,
                               master_id=master.id, start_time=DAY + timedelta(hours=hours),
                               end_time=DAY + timedelta(hours=hours + 1), status=status, price=1000, currency="EUR")
        db.add(a)
        await db.flush()
        ids.append(a.id)
    shop = SimpleNamespace(cut=cut.id, beard=beard.id, ann=ann.id, bob=bob.id, eve=eve.id,
                           clients=[c.id for c in clients], appts=ids,
                           ann_headers={"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"})
    await db.commit()
    return shop


# ------------------------------------------------------------------ клиенты
async def _names(api, org, **params):
    r = await api.get(f"{org.base}/clients", headers=org.headers, params=params)
    assert r.status_code == 200, r.text
    return [c["full_name"] for c in r.json()], int(r.headers[TOTAL])


async def test_clients_sorted_by_name_case_insensitive(api, org_a, shop):
    assert await _names(api, org_a) == (["Anna", "boris", "Clara 50%_off"], 3)
    assert await _names(api, org_a, dir="desc") == (["Clara 50%_off", "boris", "Anna"], 3)


async def test_clients_sorted_by_created_at_and_last_visit(api, org_a, shop):
    assert (await _names(api, org_a, sort="created_at"))[0] == ["Clara 50%_off", "boris", "Anna"]
    assert (await _names(api, org_a, sort="created_at", dir="desc"))[0] == ["Anna", "boris", "Clara 50%_off"]
    # последний состоявшийся визит: Anna (день 2), boris (день 1), у Clara визитов нет — всегда в конце
    assert (await _names(api, org_a, sort="last_visit", dir="desc"))[0] == ["Anna", "boris", "Clara 50%_off"]
    assert (await _names(api, org_a, sort="last_visit"))[0] == ["boris", "Anna", "Clara 50%_off"]


async def test_clients_search_and_total_ignore_paging(api, org_a, shop):
    assert await _names(api, org_a, q="ann") == (["Anna"], 1)
    assert await _names(api, org_a, q="+38067") == (["Anna", "boris"], 2)
    assert await _names(api, org_a, q="%") == (["Clara 50%_off"], 1)  # % — обычный символ, а не «всё подряд»
    assert await _names(api, org_a, q="_") == (["Clara 50%_off"], 1)
    assert await _names(api, org_a, limit=1, skip=1) == (["boris"], 3)


async def test_clients_bad_sort_is_rejected(api, org_a, shop):
    r = await api.get(f"{org_a.base}/clients", headers=org_a.headers, params={"sort": "phone"})
    assert r.status_code == 422
    r = await api.get(f"{org_a.base}/clients", headers=org_a.headers, params={"dir": "up"})
    assert r.status_code == 422


# ------------------------------------------------------------------ записи
async def _ids(api, org, headers=None, path="", **params):
    r = await api.get(f"{org.base}/appointments{path}", headers=headers or org.headers, params=params)
    assert r.status_code == 200, r.text
    return [a["id"] for a in r.json()], int(r.headers[TOTAL])


def _range():
    return {"date_from": (DAY - timedelta(days=1)).isoformat(), "date_to": (DAY + timedelta(days=3)).isoformat()}


async def test_appointments_multi_filters(api, org_a, shop):
    a = shop.appts
    assert await _ids(api, org_a) == (a, 5)
    assert await _ids(api, org_a, dir="desc") == (a[::-1], 5)
    assert await _ids(api, org_a, master_id=[shop.ann, shop.eve]) == ([a[0], a[2], a[3]], 3)
    assert await _ids(api, org_a, status=["completed", "no_show"]) == ([a[0], a[3], a[4]], 3)
    assert await _ids(api, org_a, service_id=shop.beard, master_id=shop.ann) == ([a[3]], 1)
    assert await _ids(api, org_a, client_id=shop.clients[0]) == ([a[0], a[1]], 2)
    day_two = {"date_from": (DAY + timedelta(hours=24)).isoformat()}
    assert await _ids(api, org_a, **day_two) == ([a[3], a[4]], 2)
    assert await _ids(api, org_a, limit=2, skip=1) == ([a[1], a[2]], 5)


async def test_calendar_multi_filters_and_total(api, org_a, shop):
    a = shop.appts
    assert await _ids(api, org_a, path="/calendar", **_range()) == (a, 5)
    assert await _ids(api, org_a, path="/calendar", master_id=shop.bob, **_range()) == ([a[1], a[4]], 2)
    assert await _ids(api, org_a, path="/calendar", master_id=[shop.ann, shop.bob],
                      status="scheduled", **_range()) == ([a[1]], 1)
    assert await _ids(api, org_a, path="/calendar", limit=1, **_range()) == ([a[0]], 5)


async def test_bad_status_is_rejected(api, org_a, shop):
    r = await api.get(f"{org_a.base}/appointments", headers=org_a.headers, params={"status": "done"})
    assert r.status_code == 422


async def test_master_filters_stay_inside_own_appointments(api, org_a, shop):
    a = shop.appts
    assert await _ids(api, org_a, headers=shop.ann_headers) == ([a[0], a[3]], 2)
    assert await _ids(api, org_a, headers=shop.ann_headers, master_id=[shop.ann, shop.bob]) == ([a[0], a[3]], 2)
    assert await _ids(api, org_a, headers=shop.ann_headers, path="/calendar",
                      master_id=shop.bob, **_range()) == ([], 0)
