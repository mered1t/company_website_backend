"""Письма клиенту, когда запись отменили или изменили: сам клиент по ссылке или сотрудник салона.

Кому писать: при онлайн-записи на email, который клиент оставил при записи (первый токен записи), а если такого нет,
на email из карточки клиента; язык тот, что клиент выбрал при записи. Запись из CRM: email из карточки клиента,
язык из настройки салона client_email_language (как у напоминаний).
Письма уходят в фоне после коммита; сбой отправки пишется в лог и запрос не ломает.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from fastapi import BackgroundTasks
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from services.email_service import safe_send, send_booking_cancelled_email, send_booking_changed_email
from services.i18n import DEFAULT_LANGUAGE
from services.booking_tokens import create_token


@dataclass
class Notification:
    send: Callable
    kwargs: dict
    log: tuple


def dispatch(background_tasks: BackgroundTasks, notification: "Notification | None") -> None:
    """Поставить письмо в очередь фоновых задач (вызывать после коммита)."""
    if notification is not None:
        background_tasks.add_task(safe_send, notification.send, log=notification.log, **notification.kwargs)


async def _recipient(db: AsyncSession, appointment: models.Appointment) -> tuple[str, str] | None:
    """(email, язык) или None, если писать некуда."""
    client = await db.get(models.Client, appointment.client_id)
    if client is not None and client.anonymized_at is not None:
        return None

    first_token = (await db.execute(
        select(models.AppointmentToken)
        .where(models.AppointmentToken.appointment_id == appointment.id)
        .order_by(models.AppointmentToken.id)
        .limit(1),
    )).scalars().first()

    client_email = client.email if client else None
    if appointment.booked_online and first_token:
        email = first_token.email or client_email
        return (email, first_token.language) if email else None

    # запись из CRM: токен у неё мог появиться от напоминания, но адрес и язык берём из карточки и настроек салона
    if not client_email:
        return None
    organization = await db.get(models.Organization, appointment.organization_id)
    return client_email, organization.client_email_language or DEFAULT_LANGUAGE


async def _names(db: AsyncSession, appointment: models.Appointment) -> tuple[str, str, str]:
    service = await db.get(models.Service, appointment.service_id)
    master = await db.get(models.Master, appointment.master_id)
    organization = await db.get(models.Organization, appointment.organization_id)
    return organization.name, service.name, master.full_name


async def prepare_cancelled_email(
    db: AsyncSession, appointment: models.Appointment, *, by_salon: bool,
) -> Notification | None:
    """Письмо «запись отменена». Вызывать до коммита. None, если письмо слать некому."""
    with db.no_autoflush:  # правки записи ещё не сохранены
        recipient = await _recipient(db, appointment)
        if recipient is None:
            return None
        email, language = recipient
        organization_name, service_name, master_name = await _names(db, appointment)
    return Notification(
        send=send_booking_cancelled_email,
        kwargs=dict(
            to_email=email, organization_name=organization_name, service_name=service_name,
            master_name=master_name, start_time=appointment.start_time, by_salon=by_salon, language=language,
        ),
        log=("Failed to send booking cancellation email (appointment_id=%s)", appointment.id),
    )


async def prepare_changed_email(
    db: AsyncSession, appointment: models.Appointment, *, old_start: datetime | None, by_salon: bool,
) -> Notification | None:
    """Письмо «запись изменена» с новой ссылкой управления. Вызывать до коммита (токен сохранится вместе с ним).

    old_start: прежнее время, если оно изменилось (в письме оно будет зачёркнуто).
    """
    with db.no_autoflush:
        recipient = await _recipient(db, appointment)
        if recipient is None:
            return None
        email, language = recipient
        organization_name, service_name, master_name = await _names(db, appointment)
    # сырые токены нигде не хранятся, поэтому для новой ссылки нужен новый токен (старые ссылки тоже работают)
    manage_token = await create_token(db, appointment.id, language, email)
    return Notification(
        send=send_booking_changed_email,
        kwargs=dict(
            to_email=email, organization_name=organization_name, service_name=service_name,
            master_name=master_name, start_time=appointment.start_time, manage_token=manage_token,
            old_start_time=old_start, by_salon=by_salon, language=language,
        ),
        log=("Failed to send booking change email (appointment_id=%s)", appointment.id),
    )
