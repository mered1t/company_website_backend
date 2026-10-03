from datetime import datetime, timedelta

from sqlalchemy import select, update

import models


async def register_and_login(api, username, email):
    r = await api.post("/api/users", json={
        "username": username, "email": email, "password": "Passw0rd!", "accept_terms": True,
    })
    assert r.status_code == 201, r.text
    r = await api.post("/api/users/token", data={"username": email, "password": "Passw0rd!"})
    assert r.status_code == 200, r.text
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
    me = await api.get("/api/users/me", headers=headers)
    return headers, me.json()["id"]


async def verify_in_db(db, email):
    await db.execute(update(models.User).where(models.User.email == email).values(email_verified=True))
    await db.commit()


async def make_invitation(db, org_id, email):
    inv = models.Invitation(
        organization_id=org_id, email=email, role=models.MembershipRole.admin,
        token=f"tok-{email}", expires_at=datetime.now() + timedelta(days=1),
    )
    db.add(inv)
    await db.commit()
    await db.refresh(inv)
    return inv.id


async def test_unverified_user_cannot_accept_or_see_invitations(api, db, org_a):
    headers, _ = await register_and_login(api, "mallory", "ivan@example.com")
    inv_id = await make_invitation(db, org_a.org_id, "ivan@example.com")

    assert (await api.get("/api/invitations/me/pending", headers=headers)).status_code == 403
    assert (await api.post(f"/api/invitations/{inv_id}/accept", headers=headers)).status_code == 403
    assert (await api.post(f"/api/invitations/{inv_id}/decline", headers=headers)).status_code == 403

    # членом организации он не стал
    assert (await api.get(f"{org_a.base}/clients", headers=headers)).status_code == 403


async def test_verified_user_can_accept_invitation(api, db, org_a):
    headers, _ = await register_and_login(api, "ivan", "ivan@example.com")
    await verify_in_db(db, "ivan@example.com")
    inv_id = await make_invitation(db, org_a.org_id, "ivan@example.com")

    r = await api.post(f"/api/invitations/{inv_id}/accept", headers=headers)
    assert r.status_code == 204, r.text
    assert (await api.get(f"{org_a.base}/clients", headers=headers)).status_code == 200


async def test_changing_email_resets_verification(api, db):
    headers, user_id = await register_and_login(api, "mallory", "mallory@example.com")
    await verify_in_db(db, "mallory@example.com")

    r = await api.patch(f"/api/users/{user_id}", headers=headers, json={"email": "ivan@example.com"})
    assert r.status_code == 200, r.text
    assert r.json()["email_verified"] is False


async def test_changed_email_cannot_be_used_to_accept_invitation(api, db, org_a):
    headers, user_id = await register_and_login(api, "mallory", "mallory@example.com")
    await verify_in_db(db, "mallory@example.com")
    inv_id = await make_invitation(db, org_a.org_id, "ivan@example.com")

    await api.patch(f"/api/users/{user_id}", headers=headers, json={"email": "ivan@example.com"})

    r = await api.post(f"/api/invitations/{inv_id}/accept", headers=headers)
    assert r.status_code == 403, r.text


async def test_old_verification_token_does_not_verify_new_email(api, sent_emails):
    import re

    def token_from(payload):
        return re.search(r"token=([A-Za-z0-9_\-]+)", payload["html"]).group(1)

    headers, user_id = await register_and_login(api, "mallory", "mallory@example.com")
    old_token = token_from(sent_emails[-1])

    sent_emails.clear()
    await api.patch(f"/api/users/{user_id}", headers=headers, json={"email": "ivan@example.com"})
    new_token = token_from(sent_emails[-1])
    assert new_token != old_token

    # старый токен после смены email не подтверждает ничего
    r = await api.post("/api/users/verify-email", json={"token": old_token})
    assert r.status_code == 400, r.text
    me = await api.get("/api/users/me", headers=headers)
    assert me.json()["email_verified"] is False

    # токен из нового письма работает
    r = await api.post("/api/users/verify-email", json={"token": new_token})
    assert r.status_code in (200, 204), r.text
    me = await api.get("/api/users/me", headers=headers)
    assert me.json()["email_verified"] is True


