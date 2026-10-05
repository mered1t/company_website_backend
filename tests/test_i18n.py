import re

import pytest

import email_service
from config import settings
from i18n import EMAIL_STRINGS, SUPPORTED_LANGUAGES, email_subject, normalize_language

PASSWORD = "Passw0rd!"


def test_every_language_has_every_string():
    for key, languages in EMAIL_STRINGS.items():
        assert set(languages) == set(SUPPORTED_LANGUAGES), key
        english_keys = set(languages["en"])
        for lang, strings in languages.items():
            assert set(strings) == english_keys, f"{key}/{lang}"
            assert all(v.strip() for v in strings.values()), f"{key}/{lang}"


@pytest.mark.parametrize("lang", SUPPORTED_LANGUAGES)
def test_templates_render_in_every_language(lang):
    html = email_service.render_email("verify_email.html", lang, verify_link="https://x.test/v?token=abc")
    assert f'lang="{lang}"' in html
    assert "https://x.test/v?token=abc" in html
    assert not re.search(r"{[a-z_]+}", html)

    html = email_service.render_email("password_reset.html", lang, reset_url="https://x.test/r?token=abc")
    assert "https://x.test/r?token=abc" in html

    html = email_service.render_email(
        "invitation.html", lang, organization_name="Salon <b>X</b>", accept_url="https://x.test/a?token=abc",
    )
    assert "Salon &lt;b&gt;X&lt;/b&gt;" in html
    assert "Salon <b>X</b>" not in html
    assert not re.search(r"{[a-z_]+}", html)


def test_unknown_language_falls_back_to_english():
    assert normalize_language("xx") == "en"
    assert normalize_language(None) == "en"
    assert email_subject("verify_email", "xx") == email_subject("verify_email", "en")


def test_subjects_differ_between_languages():
    subjects = {email_subject("password_reset", lang) for lang in SUPPORTED_LANGUAGES}
    assert len(subjects) == len(SUPPORTED_LANGUAGES)


async def _register(api, username, language=None):
    body = {"username": username, "email": f"{username}@example.com", "password": PASSWORD, "accept_terms": True}
    if language:
        body["language"] = language
    return await api.post("/api/v1/users", json=body)


async def test_registration_sends_email_in_chosen_language(api, sent_emails):
    r = await _register(api, "polishuser", "pl")
    assert r.status_code == 201, r.text
    assert sent_emails[-1]["subject"] == email_subject("verify_email", "pl")
    assert sent_emails[-1]["subject"] != email_subject("verify_email", "en")


async def test_registration_defaults_to_english(api, sent_emails):
    r = await _register(api, "defaultuser")
    assert r.status_code == 201, r.text
    assert sent_emails[-1]["subject"] == email_subject("verify_email", "en")


async def test_registration_rejects_unknown_language(api):
    r = await _register(api, "weirduser", "xx")
    assert r.status_code == 422


async def test_changed_language_is_used_for_password_reset(api, sent_emails):
    r = await _register(api, "spanishuser")
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    login = await api.post("/api/v1/users/token", data={"username": "spanishuser@example.com", "password": PASSWORD})
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    r = await api.patch(f"/api/v1/users/{user_id}", headers=headers, json={"language": "es"})
    assert r.status_code == 200, r.text
    me = await api.get("/api/v1/users/me", headers=headers)
    assert me.json()["language"] == "es"

    r = await api.post("/api/v1/users/forgot-password", json={"email": "spanishuser@example.com"})
    assert r.status_code < 300, r.text
    assert sent_emails[-1]["subject"] == email_subject("password_reset", "es")


def test_reply_to_settings_still_work(sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "email_reply_to", "owner@example.com")
    email_service.send_password_reset_email("user@example.com", "tok123", "uk")
    assert sent_emails[-1]["reply_to"] == "owner@example.com"
    assert sent_emails[-1]["subject"] == email_subject("password_reset", "uk")
