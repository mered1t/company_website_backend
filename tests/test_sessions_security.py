"""Сессии и смена учётных данных: сброс и смена пароля выкидывают все входы, refresh-токен ротируется,
email меняется только с паролем, а на старый адрес приходит уведомление."""
import re

import pytest
from sqlalchemy import select

import models
from auth.auth import hash_token
from core.rate_limiter import limiter
from services import email_service
from services.i18n import SUPPORTED_LANGUAGES

PASSWORD = "Passw0rd!"
NEW_PASSWORD = "NewPassw0rd!"
USERS = "/api/v1/users"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _register(api, name):
    email = f"{name}@example.com"
    r = await api.post(USERS, json={"username": name, "email": email, "password": PASSWORD, "accept_terms": True})
    assert r.status_code == 201, r.text
    return email


async def _login(api, email, password=PASSWORD):
    r = await api.post(f"{USERS}/token", data={"username": email, "password": password})
    return r


async def _tokens(api, email, password=PASSWORD):
    r = await _login(api, email, password)
    assert r.status_code == 200, r.text
    return r.json()


def _auth(tokens):
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _refresh(api, refresh_token):
    return await api.post(f"{USERS}/refresh", json={"refresh_token": refresh_token})


async def _reset_link(api, email, sent_emails):
    sent_emails.clear()
    assert (await api.post(f"{USERS}/forgot-password", json={"email": email})).status_code == 204
    return re.search(r"token=([A-Za-z0-9_\-]+)", sent_emails[-1]["html"]).group(1)


# ------------------------------------------------------------------ сброс пароля
async def test_password_reset_logs_out_every_device(api, sent_emails):
    email = await _register(api, "victim")
    laptop, phone = await _tokens(api, email), await _tokens(api, email)

    token = await _reset_link(api, email, sent_emails)
    r = await api.post(f"{USERS}/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert r.status_code == 204, r.text

    assert (await _refresh(api, laptop["refresh_token"])).status_code == 401
    assert (await _refresh(api, phone["refresh_token"])).status_code == 401
    assert (await _login(api, email)).status_code == 401
    assert (await _login(api, email, NEW_PASSWORD)).status_code == 200


async def test_using_one_reset_link_kills_the_others(api, sent_emails):
    email = await _register(api, "twolinks")
    first = await _reset_link(api, email, sent_emails)
    second = await _reset_link(api, email, sent_emails)
    assert (await api.post(f"{USERS}/reset-password", json={"token": first, "new_password": NEW_PASSWORD})).status_code == 204
    for token in (first, second):
        r = await api.post(f"{USERS}/reset-password", json={"token": token, "new_password": "Another1Pass"})
        assert r.status_code == 400
    assert (await _login(api, email, NEW_PASSWORD)).status_code == 200


async def test_expired_reset_link_does_nothing(api, db, sent_emails):
    from datetime import timedelta
    from core.time_utils import utc_now
    email = await _register(api, "latecomer")
    token = await _reset_link(api, email, sent_emails)
    row = (await db.execute(select(models.PasswordResetToken).where(
        models.PasswordResetToken.token == hash_token(token)))).scalar_one()
    row.expires_at = utc_now() - timedelta(minutes=1)
    await db.commit()
    r = await api.post(f"{USERS}/reset-password", json={"token": token, "new_password": NEW_PASSWORD})
    assert r.status_code == 400
    assert (await _login(api, email)).status_code == 200


# ------------------------------------------------------------------ ротация refresh-токена
async def test_refresh_token_is_rotated_and_the_old_one_stops_working(api, db):
    email = await _register(api, "rotator")
    first = (await _tokens(api, email))["refresh_token"]

    r = await _refresh(api, first)
    assert r.status_code == 200, r.text
    second = r.json()["refresh_token"]
    assert second != first
    assert (await api.get(f"{USERS}/me", headers=_auth(r.json()))).status_code == 200

    assert (await _refresh(api, first)).status_code == 401  # повторно старый не работает
    r = await _refresh(api, second)
    assert r.status_code == 200 and r.json()["refresh_token"] not in (first, second)

    db.expire_all()
    rows = {row.token: row for row in (await db.execute(select(models.RefreshToken))).scalars().all()}
    # срок входа не продлевается ротацией: 30 дней с момента логина
    assert rows[hash_token(second)].expires_at == rows[hash_token(first)].expires_at


async def test_expired_refresh_token_is_rejected(api, db):
    from datetime import timedelta
    from core.time_utils import utc_now
    email = await _register(api, "expired")
    refresh = (await _tokens(api, email))["refresh_token"]
    row = (await db.execute(select(models.RefreshToken).where(
        models.RefreshToken.token == hash_token(refresh)))).scalar_one()
    row.expires_at = utc_now() - timedelta(seconds=1)
    await db.commit()
    assert (await _refresh(api, refresh)).status_code == 401


# ------------------------------------------------------------------ смена пароля
async def test_change_password_keeps_this_device_and_logs_out_the_rest(api):
    email = await _register(api, "changer")
    this_device, other_device = await _tokens(api, email), await _tokens(api, email)

    r = await api.post(f"{USERS}/me/password", headers=_auth(this_device),
                       json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 200, r.text
    fresh = r.json()

    assert (await _refresh(api, other_device["refresh_token"])).status_code == 401
    assert (await _refresh(api, this_device["refresh_token"])).status_code == 401
    assert (await _refresh(api, fresh["refresh_token"])).status_code == 200
    assert (await _login(api, email)).status_code == 401
    assert (await _login(api, email, NEW_PASSWORD)).status_code == 200


async def test_change_password_needs_the_current_one(api):
    email = await _register(api, "guesser")
    tokens = await _tokens(api, email)
    r = await api.post(f"{USERS}/me/password", headers=_auth(tokens),
                       json={"current_password": "Wrong1Pass", "new_password": NEW_PASSWORD})
    assert r.status_code == 400 and r.json()["detail"] == "Incorrect password"
    assert (await _refresh(api, tokens["refresh_token"])).status_code == 200  # ничего не сломалось
    assert (await _login(api, email)).status_code == 200


@pytest.mark.parametrize("new_password", ["short1A", "nouppercase1", "NoDigitsHere"])
async def test_change_password_checks_strength(api, new_password):
    email = await _register(api, f"weak{len(new_password)}{new_password[0]}")
    tokens = await _tokens(api, email)
    r = await api.post(f"{USERS}/me/password", headers=_auth(tokens),
                       json={"current_password": PASSWORD, "new_password": new_password})
    assert r.status_code == 422


async def test_change_password_needs_login(api):
    r = await api.post(f"{USERS}/me/password", json={"current_password": PASSWORD, "new_password": NEW_PASSWORD})
    assert r.status_code == 401


# ------------------------------------------------------------------ смена email
async def _me(api, tokens):
    return (await api.get(f"{USERS}/me", headers=_auth(tokens))).json()


@pytest.mark.parametrize("body, detail", [
    ({"email": "thief@example.com"}, "Current password is required to change email"),
    ({"email": "thief@example.com", "current_password": "Wrong1Pass"}, "Incorrect password"),
])
async def test_email_cannot_be_changed_without_the_password(api, sent_emails, body, detail):
    email = await _register(api, "owner1")
    tokens = await _tokens(api, email)
    me = await _me(api, tokens)
    sent_emails.clear()
    r = await api.patch(f"{USERS}/{me['id']}", headers=_auth(tokens), json=body)
    assert r.status_code == 400 and r.json()["detail"] == detail
    assert (await _me(api, tokens))["email"] == email
    assert sent_emails == []


async def test_email_change_with_password_notifies_the_old_address(api, sent_emails):
    email = await _register(api, "mover")
    tokens = await _tokens(api, email)
    me = await _me(api, tokens)
    sent_emails.clear()
    r = await api.patch(f"{USERS}/{me['id']}", headers=_auth(tokens),
                        json={"email": "New.Address@example.com", "current_password": PASSWORD})
    assert r.status_code == 200, r.text
    assert r.json()["email"] == "new.address@example.com" and r.json()["email_verified"] is False

    by_address = {m["to"]: m for m in sent_emails}
    assert set(by_address) == {email, "new.address@example.com"}
    notice = by_address[email]
    assert notice["subject"] == "Your account email address was changed"
    assert "new.address@example.com" in notice["html"]
    assert "token=" not in notice["html"]  # в уведомление на старый адрес ссылок с токенами не кладём


async def test_name_and_language_change_without_password(api, sent_emails):
    email = await _register(api, "renamer")
    tokens = await _tokens(api, email)
    me = await _me(api, tokens)
    sent_emails.clear()
    r = await api.patch(f"{USERS}/{me['id']}", headers=_auth(tokens), json={"username": "renamed", "language": "pl"})
    assert r.status_code == 200, r.text
    same_email = await api.patch(f"{USERS}/{me['id']}", headers=_auth(tokens), json={"email": email.upper()})
    assert same_email.status_code == 200  # тот же адрес, только регистр: это не смена
    assert sent_emails == []


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_email_changed_notice_renders_and_escapes(lang):
    html = email_service.render_email("email_changed.html", lang, new_email="<b>x</b>@example.com")
    assert "&lt;b&gt;x&lt;/b&gt;@example.com" in html and "<b>x</b>@" not in html
    assert not re.search(r"{[a-z_]+}", html)
