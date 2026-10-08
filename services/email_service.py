import logging
from datetime import datetime
from pathlib import Path

import resend
from jinja2 import Environment, FileSystemLoader, select_autoescape

from core.config import settings
from services.i18n import DEFAULT_LANGUAGE, email_subject, email_texts, normalize_language

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

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "emails"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(["html"]),  # экранируем пользовательские данные
)


def _frontend_url(path: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}{path}"


def render_email(template_name: str, language: str | None = DEFAULT_LANGUAGE, **context) -> str:
    lang = normalize_language(language)
    template = _env.get_template(template_name)
    texts = email_texts(
        template_name.removesuffix(".html"),
        lang,
        app_name=settings.app_name,
        organization_name=context.get("organization_name", ""),
    )
    return template.render(
        app_name=settings.app_name,
        current_year=datetime.now().year,
        lang=lang,
        t=texts,
        **context,
    )


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


def send_invitation_email(to_email: str, organization_name: str, token: str, language: str | None = DEFAULT_LANGUAGE) -> None:
    accept_url = _frontend_url(f"/invitations/accept?token={token}")
    _send(
        to_email,
        email_subject("invitation", language, organization_name=organization_name),
        render_email("invitation.html", language, organization_name=organization_name, accept_url=accept_url),
    )


def send_password_reset_email(to_email: str, token: str, language: str | None = DEFAULT_LANGUAGE) -> None:
    reset_url = _frontend_url(f"/reset-password?token={token}")
    _send(
        to_email,
        email_subject("password_reset", language),
        render_email("password_reset.html", language, reset_url=reset_url),
    )


def send_verification_email(to_email: str, token: str, language: str | None = DEFAULT_LANGUAGE) -> None:
    verify_url = _frontend_url(f"/verify-email?token={token}")
    _send(
        to_email,
        email_subject("verify_email", language),
        render_email("verify_email.html", language, verify_link=verify_url),
    )


def send_booking_confirmation_email(
    to_email: str,
    organization_name: str,
    service_name: str,
    master_name: str,
    start_time: datetime,
    manage_token: str,
    language: str | None = DEFAULT_LANGUAGE,
) -> None:
    manage_url = _frontend_url(f"/booking/manage?token={manage_token}")
    _send(
        to_email,
        email_subject("booking_confirmation", language, organization_name=organization_name),
        render_email(
            "booking_confirmation.html",
            language,
            organization_name=organization_name,
            service_name=service_name,
            master_name=master_name,
            when=start_time.strftime("%d.%m.%Y %H:%M"),
            manage_url=manage_url,
        ),
    )


def send_booking_reminder_email(
    to_email: str,
    organization_name: str,
    service_name: str,
    master_name: str,
    start_time: datetime,
    manage_token: str,
    language: str | None = DEFAULT_LANGUAGE,
) -> None:
    manage_url = _frontend_url(f"/booking/manage?token={manage_token}")
    _send(
        to_email,
        email_subject("booking_reminder", language, organization_name=organization_name),
        render_email(
            "booking_reminder.html",
            language,
            organization_name=organization_name,
            service_name=service_name,
            master_name=master_name,
            when=start_time.strftime("%d.%m.%Y %H:%M"),
            manage_url=manage_url,
        ),
    )


def send_booking_cancelled_email(
    to_email: str,
    organization_name: str,
    service_name: str,
    master_name: str,
    start_time: datetime,
    by_salon: bool = False,
    language: str | None = DEFAULT_LANGUAGE,
) -> None:
    _send(
        to_email,
        email_subject("booking_cancelled", language, organization_name=organization_name),
        render_email(
            "booking_cancelled.html",
            language,
            organization_name=organization_name,
            service_name=service_name,
            master_name=master_name,
            when=start_time.strftime("%d.%m.%Y %H:%M"),
            by_salon=by_salon,
        ),
    )


def send_booking_changed_email(
    to_email: str,
    organization_name: str,
    service_name: str,
    master_name: str,
    start_time: datetime,
    manage_token: str,
    old_start_time: datetime | None = None,
    by_salon: bool = False,
    language: str | None = DEFAULT_LANGUAGE,
) -> None:
    manage_url = _frontend_url(f"/booking/manage?token={manage_token}")
    _send(
        to_email,
        email_subject("booking_changed", language, organization_name=organization_name),
        render_email(
            "booking_changed.html",
            language,
            organization_name=organization_name,
            service_name=service_name,
            master_name=master_name,
            when=start_time.strftime("%d.%m.%Y %H:%M"),
            old_when=old_start_time.strftime("%d.%m.%Y %H:%M") if old_start_time else None,
            manage_url=manage_url,
            by_salon=by_salon,
        ),
    )


def safe_send(func, *, log: tuple, **kwargs) -> None:
    """Отправка письма в фоне: ошибка пишется в лог (и в Sentry), но не ломает запрос.

    log — аргументы для logger.exception: ("сообщение %s", значение, ...).
    """
    try:
        func(**kwargs)
    except Exception:
        logger.exception(*log)
