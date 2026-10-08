from services.email_service import (
    send_invitation_email,
    send_password_reset_email,
    send_verification_email,
)


def _html(sent_emails) -> str:
    assert len(sent_emails) == 1
    return sent_emails[0]["html"]


def test_verification_email_contains_working_link(sent_emails):
    send_verification_email(to_email="a@example.com", token="tok123")
    html = _html(sent_emails)
    assert "/verify-email?token=tok123" in html
    assert 'href=""' not in html


def test_password_reset_email_contains_working_link(sent_emails):
    send_password_reset_email(to_email="a@example.com", token="tok123")
    html = _html(sent_emails)
    assert "/reset-password?token=tok123" in html
    assert 'href=""' not in html


def test_invitation_email_contains_working_link(sent_emails):
    send_invitation_email(to_email="a@example.com", organization_name="Shop", token="tok123")
    html = _html(sent_emails)
    assert "/invitations/accept?token=tok123" in html
    assert 'href=""' not in html