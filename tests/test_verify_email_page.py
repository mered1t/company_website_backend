"""Ссылка «подтвердить почту» ведёт на страницу бэкенда (если задан BACKEND_URL): кнопка, затем подтверждение."""
import re

import pytest
from sqlalchemy import select

import models
from core.config import settings
from core.rate_limiter import limiter
from services.email_service import send_verification_email

USERS = "/api/v1/users"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _register(api, sent_emails, monkeypatch, language="uk"):
    monkeypatch.setattr(settings, "backend_url", "https://api.example.com/")
    r = await api.post(USERS, json={"username": "verify-me", "email": "verify-me@example.com",
                                    "password": "Passw0rd!", "accept_terms": True, "language": language})
    assert r.status_code == 201, r.text
    mail = sent_emails[-1]
    link = re.search(r'href="(https://api\.example\.com/api/v1/users/verify-email\?token=[^"]+)"', mail["html"])
    assert link, mail["html"]
    return link.group(1).replace("https://api.example.com", "")


async def _verified(db):
    db.expire_all()
    user = (await db.execute(select(models.User).where(models.User.email == "verify-me@example.com"))).scalar_one()
    return user.email_verified


async def test_link_opens_a_page_and_the_button_confirms(api, db, sent_emails, monkeypatch):
    path = await _register(api, sent_emails, monkeypatch)
    page = await api.get(path)
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "Підтвердити пошту" in page.text and 'action="/api/v1/users/verify-email/confirm"' in page.text
    assert await _verified(db) is False  # открытие ссылки ничего не подтверждает (почтовые сканеры)

    token = re.search(r'name="token" value="([^"]+)"', page.text).group(1)
    done = await api.post(f"{USERS}/verify-email/confirm", data={"token": token})
    assert done.status_code == 200 and "Пошту підтверджено" in done.text
    assert await _verified(db) is True

    again = await api.post(f"{USERS}/verify-email/confirm", data={"token": token})
    assert again.status_code == 200 and "Посилання недійсне" in again.text


async def test_unknown_token_shows_an_error_in_english(api):
    page = await api.get(f"{USERS}/verify-email", params={"token": "nope"})
    assert page.status_code == 200 and "Confirm your email" in page.text
    done = await api.post(f"{USERS}/verify-email/confirm", data={"token": "nope"})
    assert "Link is no longer valid" in done.text


async def test_token_is_escaped_in_the_page(api):
    page = await api.get(f"{USERS}/verify-email", params={"token": '"><script>alert(1)</script>'})
    assert "<script>alert(1)</script>" not in page.text


def test_without_backend_url_the_link_goes_to_the_frontend(sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "backend_url", None)
    send_verification_email("a@example.com", "tok123", "en")
    assert "/verify-email?token=tok123" in sent_emails[-1]["html"]
    assert "/api/v1/users/verify-email" not in sent_emails[-1]["html"]


async def test_json_endpoint_still_works(api, db, sent_emails, monkeypatch):
    path = await _register(api, sent_emails, monkeypatch)
    token = path.split("token=")[1]
    r = await api.post(f"{USERS}/verify-email", json={"token": token})
    assert r.status_code == 204 and await _verified(db) is True
    assert (await api.post(f"{USERS}/verify-email", json={"token": token})).status_code == 400
