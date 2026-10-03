import re

import models
from sqlalchemy import select

from auth.auth import hash_token

PASSWORD = "Passw0rd!"


def token_from(email_payload) -> str:
    match = re.search(r"token=([A-Za-z0-9_\-]+)", email_payload["html"])
    assert match, f"в письме нет токена: {email_payload}"
    return match.group(1)


async def _register(api, name: str) -> str:
    email = f"{name}@example.com"
    r = await api.post("/api/users", json={
        "username": name, "email": email, "password": PASSWORD, "accept_terms": True,
    })
    assert r.status_code in (200, 201), r.text
    return email


async def test_verification_token_is_stored_hashed_and_works(api, db, sent_emails):
    await _register(api, "hashverify")
    token = token_from(sent_emails[-1])

    stored = (await db.execute(select(models.EmailVerificationToken.token))).scalars().all()
    assert token not in stored
    assert hash_token(token) in stored

    r = await api.post("/api/users/verify-email", json={"token": token})
    assert r.status_code in (200, 204), r.text

    # повторно тот же токен использовать нельзя
    r = await api.post("/api/users/verify-email", json={"token": token})
    assert r.status_code == 400


async def test_stored_hash_cannot_be_used_as_token(api, db, sent_emails):
    await _register(api, "hashreplay")
    stored = (await db.execute(select(models.EmailVerificationToken.token))).scalars().first()

    # даже если хеш утёк из базы, подставить его как токен нельзя
    r = await api.post("/api/users/verify-email", json={"token": stored})
    assert r.status_code in (400, 404, 422)


async def test_password_reset_flow_with_hashed_token(api, db, sent_emails):
    email = await _register(api, "resetflow")
    sent_emails.clear()

    r = await api.post("/api/users/forgot-password", json={"email": email})
    assert r.status_code == 204
    token = token_from(sent_emails[-1])

    stored = (await db.execute(select(models.PasswordResetToken.token))).scalars().all()
    assert token not in stored
    assert hash_token(token) in stored

    r = await api.post("/api/users/reset-password", json={"token": token, "new_password": "NewPassw0rd!"})
    assert r.status_code == 204

    r = await api.post("/api/users/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 401
    r = await api.post("/api/users/token", data={"username": email, "password": "NewPassw0rd!"})
    assert r.status_code == 200

    # токен одноразовый
    r = await api.post("/api/users/reset-password", json={"token": token, "new_password": "Another1Passw0rd!"})
    assert r.status_code == 400


async def test_refresh_token_is_stored_hashed(api, db):
    email = await _register(api, "refreshhash")
    r = await api.post("/api/users/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200
    refresh = r.json()["refresh_token"]

    stored = (await db.execute(select(models.RefreshToken.token))).scalars().all()
    assert refresh not in stored
    assert hash_token(refresh) in stored

async def test_refresh_and_logout_flow_with_hashed_tokens(api):
    email = await _register(api, "refreshflow")
    r = await api.post("/api/users/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200
    refresh = r.json()["refresh_token"]

    r = await api.post("/api/users/refresh", json={"refresh_token": refresh})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    # если refresh ротирует токен, берём новый, иначе остаётся прежний
    current = r.json().get("refresh_token") or refresh

    r = await api.post("/api/users/logout", json={"refresh_token": current})
    assert r.status_code == 204, r.text

    # после logout токен больше не работает
    r = await api.post("/api/users/refresh", json={"refresh_token": current})
    assert r.status_code in (400, 401)


async def test_refresh_rejects_unknown_token(api):
    r = await api.post("/api/users/refresh", json={"refresh_token": "definitely-not-a-real-token"})
    assert r.status_code in (400, 401, 422)
