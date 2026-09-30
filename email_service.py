import resend

from config import settings

from datetime import datetime
from jinja2 import Environment, FileSystemLoader

resend.api_key = settings.resend_api_key

_env = Environment(loader=FileSystemLoader("templates/emails"))

def render_email(template_name: str, **context) -> str:
    template = _env.get_template(template_name)
    return template.render(app_name="Твоё название CRM", current_year=datetime.now().year, **context)


def send_invitation_email(to_email: str, organization_name: str, token: str) -> None:
    accept_url = f"https://koracrm.com/invitations/accept?token={token}"

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": to_email,
        "subject": f"You've been invited to join {organization_name}",
        "html": render_email(
            "invitation.html",
            organization_name=organization_name,
            accept_url=accept_url,
        ),
    })


def send_password_reset_email(to_email: str, token: str) -> None:
    reset_url = f"https://koracrm.com/reset-password?token={token}"

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": to_email,
        "subject": "Reset your password",
        "html": render_email(
            "password_reset.html",
            reset_url=reset_url,
        ),
    })


def send_verification_email(to_email: str, token: str) -> None:
    verify_url = f"https://koracrm.com/verify-email?token={token}"

    resend.Emails.send({
        "from": "onboarding@resend.dev",
        "to": to_email,
        "subject": "Confirm your email",
        "html": render_email(
            "verify_email.html",
            verify_url=verify_url,
        ),
    })