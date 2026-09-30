import resend

import email_service
from config import settings


def test_email_escapes_html_in_organization_name():
    html = email_service.render_email(
        "invitation.html",
        organization_name="<script>alert(1)</script>",
        accept_url="https://x.test/a?token=abc",
    )
    assert "<script>alert(1)</script>" not in html


def test_sender_and_links_come_from_settings(monkeypatch):
    sent = []
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda params: sent.append(params)))
    monkeypatch.setattr(settings, "email_from", "Kora <noreply@kora.test>")
    monkeypatch.setattr(settings, "frontend_url", "https://app.kora.test/")

    email_service.send_password_reset_email("user@example.com", "tok123")

    assert sent[0]["from"] == "Kora <noreply@kora.test>"
    assert "https://app.kora.test/reset-password?token=tok123" in sent[0]["html"]


async def test_forgot_password_survives_email_failure(api, org_a, monkeypatch):
    def boom(params):
        raise RuntimeError("resend is down")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(boom))
    r = await api.post("/api/users/forgot-password", json={"email": "ownera@example.com"})
    assert r.status_code == 204