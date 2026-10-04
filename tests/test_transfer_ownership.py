import models

PASSWORD = "Passw0rd!"


async def _add_member(api, db, org, username, role):
    email = f"{username}@example.com"
    r = await api.post("/api/v1/users", json={
        "username": username, "email": email, "password": PASSWORD, "accept_terms": True,
    })
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    db.add(models.Membership(user_id=user_id, organization_id=org.org_id, role=role))
    await db.commit()
    return user_id, email


async def _login(api, email):
    r = await api.post("/api/v1/users/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _roles(api, org):
    r = await api.get(f"{org.base}/members", headers=org.headers)
    assert r.status_code == 200, r.text
    return {m["user_id"]: m["role"] for m in r.json()}


async def test_transfer_ownership_swaps_roles(api, db, org_a):
    new_id, _ = await _add_member(api, db, org_a, "newowner", models.MembershipRole.admin)
    before = await _roles(api, org_a)
    old_owner_id = next(uid for uid, role in before.items() if role == "owner")

    r = await api.post(f"{org_a.base}/transfer-ownership", headers=org_a.headers,
                       json={"new_owner_user_id": new_id, "password": PASSWORD})
    assert r.status_code == 204, r.text

    after = await _roles(api, org_a)
    assert after[new_id] == "owner"
    assert after[old_owner_id] == "admin"

    again = await api.post(f"{org_a.base}/transfer-ownership", headers=org_a.headers,
                           json={"new_owner_user_id": new_id, "password": PASSWORD})
    assert again.status_code == 403


async def test_transfer_requires_correct_password(api, db, org_a):
    new_id, _ = await _add_member(api, db, org_a, "newowner2", models.MembershipRole.admin)
    r = await api.post(f"{org_a.base}/transfer-ownership", headers=org_a.headers,
                       json={"new_owner_user_id": new_id, "password": "wrong-password"})
    assert r.status_code == 400
    assert (await _roles(api, org_a))[new_id] == "admin"


async def test_cannot_transfer_to_self(api, org_a):
    roles = await _roles(api, org_a)
    owner_id = next(uid for uid, role in roles.items() if role == "owner")
    r = await api.post(f"{org_a.base}/transfer-ownership", headers=org_a.headers,
                       json={"new_owner_user_id": owner_id, "password": PASSWORD})
    assert r.status_code == 400


async def test_cannot_transfer_to_non_member(api, org_a):
    r = await api.post(f"{org_a.base}/transfer-ownership", headers=org_a.headers,
                       json={"new_owner_user_id": 999999, "password": PASSWORD})
    assert r.status_code == 404


async def test_admin_cannot_transfer_ownership(api, db, org_a):
    admin_id, admin_email = await _add_member(api, db, org_a, "justadmin", models.MembershipRole.admin)
    headers = await _login(api, admin_email)
    r = await api.post(f"{org_a.base}/transfer-ownership", headers=headers,
                       json={"new_owner_user_id": admin_id, "password": PASSWORD})
    assert r.status_code == 403
