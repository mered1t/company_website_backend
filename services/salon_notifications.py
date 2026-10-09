"""Письма салону, когда клиент сам отменил или перенёс запись по ссылке из письма.

Кому: владельцам и администраторам организации и мастеру этой записи (если у мастера есть учётная запись).
Один адрес получает письмо один раз (даже если у двух пользователей он отличается только регистром). Язык: язык пользователя.
Когда запись отменяет или переносит сам салон, эти письма не шлются (сотрудник и так знает).
Закрытым организациям (льготный период закончился или приостановлены) письма не шлются.
Письма уходят в фоне после коммита; сбой отправки пишется в лог и запрос клиента не ломает.
"""
from datetime import datetime

from fastapi import BackgroundTasks
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from domain.subscription import get_subscription
from services.booking_notifications import Notification, dispatch
from services.email_service import _frontend_url, _send, render_email
from services.i18n import DEFAULT_LANGUAGE, email_subject


def _format(value: datetime) -> str:
    return value.strftime("%d.%m.%Y %H:%M")


def send_salon_booking_email(
    to_email: str,
    key: str,
    organization_name: str,
    client_name: str,
    client_phone: str,
    service_name: str,
    master_name: str,
    when: str,
    old_when: str | None = None,
    language: str | None = DEFAULT_LANGUAGE,
) -> None:
    _send(
        to_email,
        email_subject(key, language, organization_name=organization_name),
        render_email(
            f"{key}.html",
            language,
            organization_name=organization_name,
            client_name=client_name,
            client_phone=client_phone,
            service_name=service_name,
            master_name=master_name,
            when=when,
            old_when=old_when,
            open_url=_frontend_url("/"),
        ),
    )


async def _recipients(db: AsyncSession, appointment: models.Appointment) -> list[tuple[str, str]]:
    """(email, язык) без повторов."""
    rows = (await db.execute(
        select(models.User.email, models.User.language)
        .join(models.Membership, models.Membership.user_id == models.User.id)
        .where(
            models.Membership.organization_id == appointment.organization_id,
            or_(
                models.Membership.role.in_([models.MembershipRole.owner, models.MembershipRole.admin]),
                and_(
                    models.Membership.role == models.MembershipRole.master,
                    models.Membership.master_id == appointment.master_id,
                ),
            ),
        )
        .order_by(models.Membership.id),
    )).all()
    seen: set[str] = set()
    result = []
    for email, language in rows:
        if email and email.lower() not in seen:
            seen.add(email.lower())
            result.append((email, language))
    return result


async def _prepare(
    db: AsyncSession, appointment: models.Appointment, key: str, old_start: datetime | None,
) -> list[Notification]:
    with db.no_autoflush:  # правки записи ещё не сохранены
        organization = await db.get(models.Organization, appointment.organization_id)
        if organization is None or not get_subscription(organization).has_access:
            return []
        recipients = await _recipients(db, appointment)
        if not recipients:
            return []
        client = await db.get(models.Client, appointment.client_id)
        service = await db.get(models.Service, appointment.service_id)
        master = await db.get(models.Master, appointment.master_id)
        common = dict(
            key=key,
            organization_name=organization.name,
            client_name=client.full_name if client else "",
            client_phone=client.phone if client else "",
            service_name=service.name if service else "",
            master_name=master.full_name if master else "",
            when=_format(appointment.start_time),
            old_when=_format(old_start) if old_start else None,
        )
    return [
        Notification(
            send=send_salon_booking_email,
            kwargs=dict(common, to_email=email, language=language),
            log=("Failed to send salon notification (appointment_id=%s, key=%s)", appointment.id, key),
        )
        for email, language in recipients
    ]


async def prepare_salon_cancelled(db: AsyncSession, appointment: models.Appointment) -> list[Notification]:
    """«Клиент отменил запись». Вызывать до коммита."""
    return await _prepare(db, appointment, "salon_booking_cancelled", None)


async def prepare_salon_rescheduled(
    db: AsyncSession, appointment: models.Appointment, *, old_start: datetime,
) -> list[Notification]:
    """«Клиент перенёс запись». Вызывать до коммита; в письме новое время и прежнее."""
    return await _prepare(db, appointment, "salon_booking_rescheduled", old_start)


def dispatch_all(background_tasks: BackgroundTasks, notifications: list[Notification]) -> None:
    """Поставить письма в очередь фоновых задач (вызывать после коммита)."""
    for notification in notifications:
        dispatch(background_tasks, notification)
