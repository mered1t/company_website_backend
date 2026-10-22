"""Статусы клиентов: справочник салона и ручная установка статуса, фильтр и сортировка в списке клиентов."""
import models
from auth.auth import create_access_token

UK_DEFAULTS = ["Новий", "Постійний", "VIP", "Разовий", "Сплячий", "Втрачений", "Буфер"]


async def _statuses(api, org):
    r = await api.get(f"{org.base}/client-statuses", headers=org.headers)
    assert r.status_code == 200, r.text
    return r.json()


async def _client(api, org, name, phone, **extra):
    r = await api.post(f"{org.base}/clients", headers=org.headers,
                       json={"full_name": name, "phone": phone, **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def _master_headers(db, org):
    user = models.User(username="st-master", email="st-master@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole.master))
    await db.commit()
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


async def test_new_salon_gets_default_statuses_in_owner_language(api):
    r = await api.post("/api/v1/users", json={"username": "uk-owner", "email": "uk-owner@example.com",
                                               "password": "Passw0rd!", "accept_terms": True, "language": "uk"})
    assert r.status_code == 201
    token = (await api.post("/api/v1/users/token",
                            data={"username": "uk-owner@example.com", "password": "Passw0rd!"})).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    org = (await api.post("/api/v1/organizations", headers=headers, json={"name": "Салон"})).json()
    rows = (await api.get(f"/api/v1/organizations/{org['id']}/client-statuses", headers=headers)).json()
    assert [s["name"] for s in rows] == UK_DEFAULTS
    assert all(s["color"].startswith("#") and s["clients_count"] == 0 for s in rows)


async def test_salon_edits_its_own_list(api, org_a, org_b):
    url = f"{org_a.base}/client-statuses"
    added = await api.post(url, headers=org_a.headers, json={"name": "  Абонемент ", "color": "#123ABC"})
    assert added.status_code == 201 and added.json()["name"] == "Абонемент" and added.json()["position"] == 7
    assert (await api.post(url, headers=org_a.headers, json={"name": "абонемент"})).status_code == 400
    assert (await api.post(url, headers=org_a.headers, json={"name": "X", "color": "red"})).status_code == 422
    assert (await api.post(url, headers=org_a.headers, json={"name": "x" * 51})).status_code == 422

    sid = added.json()["id"]
    r = await api.patch(f"{url}/{sid}", headers=org_a.headers, json={"name": "Абонемент 10", "color": None})
    assert r.status_code == 200 and r.json()["name"] == "Абонемент 10" and r.json()["color"] is None
    assert (await api.patch(f"{url}/{sid}", headers=org_a.headers, json={"name": None})).status_code == 422

    ids = [s["id"] for s in await _statuses(api, org_a)]
    r = await api.put(f"{url}/order", headers=org_a.headers, json={"ids": list(reversed(ids))})
    assert r.status_code == 200 and [s["id"] for s in r.json()] == list(reversed(ids))
    assert (await api.put(f"{url}/order", headers=org_a.headers, json={"ids": ids[:-1]})).status_code == 400

    # чужой салон не видит и не меняет
    assert (await api.patch(f"{url}/{sid}", headers=org_b.headers, json={"name": "Hack"})).status_code in (403, 404)
    other = await api.patch(f"{org_b.base}/client-statuses/{sid}", headers=org_b.headers, json={"name": "Hack"})
    assert other.status_code == 404


async def test_status_on_client_filter_and_sort(api, org_a):
    statuses = await _statuses(api, org_a)
    new, regular, vip = statuses[0]["id"], statuses[1]["id"], statuses[2]["id"]
    anna = await _client(api, org_a, "Anna", "+380670001101", status_id=vip)
    assert anna["status"] == {"id": vip, "name": statuses[2]["name"], "color": statuses[2]["color"]}
    bob = await _client(api, org_a, "Bob", "+380670001102", status_id=regular)
    carl = await _client(api, org_a, "Carl", "+380670001103")
    assert carl["status"] is None and carl["status_id"] is None

    clients = f"{org_a.base}/clients"
    r = await api.get(clients, headers=org_a.headers, params={"status_id": [vip, regular]})
    assert {c["full_name"] for c in r.json()} == {"Anna", "Bob"} and r.headers["x-total-count"] == "2"
    r = await api.get(clients, headers=org_a.headers, params={"status_id": [0]})
    assert [c["full_name"] for c in r.json()] == ["Carl"]
    r = await api.get(clients, headers=org_a.headers, params={"status_id": [0, vip], "q": "a"})
    assert {c["full_name"] for c in r.json()} == {"Anna", "Carl"}

    by_status = await api.get(clients, headers=org_a.headers, params={"sort": "status"})
    assert [c["full_name"] for c in by_status.json()] == ["Bob", "Anna", "Carl"]  # Постоянный, VIP, без статуса
    desc = await api.get(clients, headers=org_a.headers, params={"sort": "status", "dir": "desc"})
    assert [c["full_name"] for c in desc.json()] == ["Anna", "Bob", "Carl"]

    r = await api.patch(f"{clients}/{bob['id']}", headers=org_a.headers, json={"status_id": new})
    assert r.status_code == 200 and r.json()["status"]["id"] == new
    r = await api.patch(f"{clients}/{bob['id']}", headers=org_a.headers, json={"status_id": None})
    assert r.status_code == 200 and r.json()["status"] is None
    assert (await api.patch(f"{clients}/{bob['id']}", headers=org_a.headers, json={"status_id": 999999})).status_code == 400
    counts = {s["id"]: s["clients_count"] for s in await _statuses(api, org_a)}
    assert counts[vip] == 1 and counts[regular] == 0


async def test_foreign_status_is_refused(api, org_a, org_b):
    foreign = (await _statuses(api, org_b))[0]["id"]
    r = await api.post(f"{org_a.base}/clients", headers=org_a.headers,
                       json={"full_name": "X", "phone": "+380670001104", "status_id": foreign})
    assert r.status_code == 400


async def test_bulk_status_and_masters(api, db, org_a, org_b):
    vip = (await _statuses(api, org_a))[2]["id"]
    ids = [(await _client(api, org_a, f"C{i}", f"+38067000120{i}"))["id"] for i in range(3)]
    stranger = (await _client(api, org_b, "Other", "+380670001299"))["id"]
    headers = await _master_headers(db, org_a)  # мастер тоже ставит статусы
    r = await api.post(f"{org_a.base}/clients/bulk-status", headers=headers,
                       json={"ids": ids + [stranger], "status_id": vip})
    assert r.status_code == 200 and r.json() == {"updated": 3}
    listed = await api.get(f"{org_a.base}/clients", headers=org_a.headers, params={"status_id": [vip]})
    assert listed.headers["x-total-count"] == "3"
    r = await api.post(f"{org_a.base}/clients/bulk-status", headers=org_a.headers, json={"ids": ids[:1], "status_id": None})
    assert r.json() == {"updated": 1}
    # справочник мастер не меняет
    assert (await api.post(f"{org_a.base}/client-statuses", headers=headers, json={"name": "M"})).status_code == 403


async def test_deleting_a_status_clears_it_from_clients(api, org_a):
    vip = (await _statuses(api, org_a))[2]["id"]
    anna = await _client(api, org_a, "Anna", "+380670001301", status_id=vip)
    assert (await api.delete(f"{org_a.base}/client-statuses/{vip}", headers=org_a.headers)).status_code == 204
    r = await api.get(f"{org_a.base}/clients/{anna['id']}", headers=org_a.headers)
    assert r.json()["status"] is None and r.json()["status_id"] is None
    assert vip not in [s["id"] for s in await _statuses(api, org_a)]


async def test_csv_keeps_the_status(api, org_a):
    vip = (await _statuses(api, org_a))[2]
    await _client(api, org_a, "Anna", "+380670001401", status_id=vip["id"])
    csv_text = (await api.get(f"{org_a.base}/clients/export", headers=org_a.headers)).text
    assert csv_text.splitlines()[0].endswith("status") and vip["name"] in csv_text
    upload = "full_name,phone,status\nBob,+380670001402,vip\nCarl,+380670001403,Unknown\n"
    r = await api.post(f"{org_a.base}/clients/import", headers=org_a.headers,
                       files={"file": ("c.csv", upload.encode(), "text/csv")})
    assert r.status_code == 200 and r.json()["created"] == 2
    rows = {c["full_name"]: c["status_id"] for c in
            (await api.get(f"{org_a.base}/clients", headers=org_a.headers)).json()}
    assert rows["Bob"] == vip["id"] and rows["Carl"] is None
