import logging
from datetime import datetime
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader, select_autoescape

from config import settings

logger = logging.getLogger(__name__)

resend.api_key = settings.resend_api_key


def warn_if_sandbox_sender() -> None:
    """В production письма с resend.dev доходят только владельцу аккаунта Resend."""
    if settings.environment == "production" and "resend.dev" in settings.email_from:
        logger.warning(
            "EMAIL_FROM uses the Resend sandbox domain (%s); real users will not receive emails. "
            "Verify your own domain in Resend and set EMAIL_FROM.",
            settings.email_from,
        )


warn_if_sandbox_sender()

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates" / "emails"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),  # экранируем пользовательские данные
)


def _frontend_url(path: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}{path}"


def render_email(template_name: str, **context) -> str:
    template = _env.get_template(template_name)
    return template.render(app_name=settings.app_name, current_year=datetime.now().year, **context)


def _send(to_email: str, subject: str, html: str) -> None:
    payload = {
        "from": settings.email_from,
        "to": to_email,
        "subject": subject,
        "html": html,
    }
    if settings.email_reply_to:
        payload["reply_to"] = settings.email_reply_to
    resend.Emails.send(payload)


def send_invitation_email(to_email: str, organization_name: str, token: str) -> None:
    accept_url = _frontend_url(f"/invitations/accept?token={token}")
    _send(
        to_email,
        f"You've been invited to join {organization_name}",
        render_email("invitation.html", organization_name=organization_name, accept_url=accept_url),
    )


def send_password_reset_email(to_email: str, token: str) -> None:
    reset_url = _frontend_url(f"/reset-password?token={token}")
    _send(to_email, "Reset your password", render_email("password_reset.html", reset_url=reset_url))


def send_verification_email(to_email: str, token: str) -> None:
    verify_url = _frontend_url(f"/verify-email?token={token}")
    _send(to_email, "Confirm your email", render_email("verify_email.html", verify_link=verify_url))


def safe_send(func, *, log: tuple, **kwargs) -> None:
    """Отправка письма в фоне: ошибка пишется в лог (и в Sentry), но не ломает запрос.

    log — аргументы для logger.exception: ("сообщение %s", значение, ...).
    """
    try:
        func(**kwargs)
    except Exception:
        logger.exception(*log)
