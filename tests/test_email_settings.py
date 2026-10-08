import logging

from services import email_service
from core.config import settings


def test_reply_to_added_when_configured(sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "email_reply_to", "owner@example.com")
    email_service.send_password_reset_email("user@example.com", "tok123")
    assert sent_emails[-1]["reply_to"] == "owner@example.com"


def test_reply_to_absent_by_default(sent_emails, monkeypatch):
    monkeypatch.setattr(settings, "email_reply_to", None)
    email_service.send_password_reset_email("user@example.com", "tok123")
    assert "reply_to" not in sent_emails[-1]


def test_warning_when_using_resend_sandbox_in_production(monkeypatch, caplog):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "email_from", "onboarding@resend.dev")
    with caplog.at_level(logging.WARNING):
        email_service.warn_if_sandbox_sender()
    assert "resend.dev" in caplog.text


def test_no_warning_with_own_domain(monkeypatch, caplog):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "email_from", "Clientum <notify@mail.clientum.app>")
    with caplog.at_level(logging.WARNING):
        email_service.warn_if_sandbox_sender()
    assert "resend.dev" not in caplog.text
