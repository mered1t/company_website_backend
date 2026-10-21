"""Ссылка «сбросить пароль» ведёт на страницу бэкенда (если задан BACKEND_URL): форма нового пароля."""
import re

import pytest

from core.config import settings
from core.rate_limiter import limiter

USERS = "/api/v1/users"
EMAIL = "reset-me@example.com"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _link(api, sent_emails, monkeypatch, language="uk"):
    monkeypatch.setattr(settings, "backend_url", "https://api.example.com/")
    r = await api.post(USERS, json={"username": "reset-me", "email": EMAIL, "password": "Passw0rd!",
                                    "accept_terms": True, "language": language})
    assert r.status_code == 201, r.text
    assert (await api.post(f"{USERS}/forgot-password", json={"email": EMAIL})).status_code == 204
    mail = sent_emails[-1]
    link = re.search(r'href="https://api\.example\.com(/api/v1/users/reset-password\?token=([^"]+))"', mail["html"])
    assert link, mail["html"]
    return link.group(1), link.group(2)


async def _login(api, password):
    return (await api.post(f"{USERS}/token", data={"username": EMAIL, "password": password})).status_code


async def test_form_sets_the_new_password(api, sent_emails, monkeypatch):
    path, token = await _link(api, sent_emails, monkeypatch)
    page = await api.get(path)
    assert page.status_code == 200 and "Новий пароль" in page.text
    assert 'action="/api/v1/users/reset-password/confirm"' in page.text and "#0A0B10" in page.text

    confirm = f"{USERS}/reset-password/confirm"
    r = await api.post(confirm, data={"token": token, "new_password": "NewPass123", "repeat_password": "Other123"})
    assert "Паролі не збігаються" in r.text
    r = await api.post(confirm, data={"token": token, "new_password": "weakpass1", "repeat_password": "weakpass1"})
    assert "велику літеру" in r.text
    assert await _login(api, "Passw0rd!") == 200  # ошибки в форме ссылку не тратят

    r = await api.post(confirm, data={"token": token, "new_password": "NewPass123", "repeat_password": "NewPass123"})
    assert r.status_code == 200 and "Пароль змінено" in r.text
    assert await _login(api, "NewPass123") == 200 and await _login(api, "Passw0rd!") == 401

    again = await api.post(confirm, data={"token": token, "new_password": "NewPass456", "repeat_password": "NewPass456"})
    assert "Посилання недійсне" in again.text


async def test_bad_link_and_escaping(api):
    page = await api.get(f"{USERS}/reset-password", params={"token": "nope"})
    assert page.status_code == 200 and "Link is no longer valid" in page.text
    page = await api.get(f"{USERS}/reset-password", params={"token": '"><script>alert(1)</script>'})
    assert "<script>" not in page.text


async def test_without_backend_url_the_link_goes_to_the_frontend(api, sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "backend_url", None)
    monkeypatch.setattr(settings, "frontend_url", "https://app.example.com")
    await api.post(USERS, json={"username": "reset-me", "email": EMAIL, "password": "Passw0rd!", "accept_terms": True})
    await api.post(f"{USERS}/forgot-password", json={"email": EMAIL})
    assert "https://app.example.com/reset-password?token=" in sent_emails[-1]["html"]
