"""Лист ожидания: письмо клиенту, когда на нужный ему день освободилось время.

Проверка идёт в момент, когда слот освобождается (отмена, удаление, перенос записи). Письмо получают все, кто ждёт
этот день и для кого теперь есть свободное время; бронь не держится — кто первый запишется, тот и успел.
Каждая заявка получает письмо один раз.
"""
from datetime import date

from fastapi import BackgroundTasks
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.time_utils import utc_now
from domain.subscription import get_subscription
from services.booking import get_free_slots, masters_for_service
from services.common import get_org_now
from services.email_service import safe_send, send_waitlist_slot_free_email

MAX_ACTIVE_PER_PHONE = 3  # активных заявок с одного телефона в одном салоне


async def _has_free_time(db: AsyncSession, entry: models.WaitlistEntry, service: models.Service, now) -> bool:
    if entry.master_id is not None:
        master_ids = [entry.master_id]
    else:
        master_ids = [m.id for m in await masters_for_service(db, entry.organization_id, entry.service_id)]
    for master_id in master_ids:
        if await get_free_slots(db, master_id, service.duration_minutes, entry.day, now, first_only=True):
            return True
    return False


async def prepare_waitlist_emails(
    db: AsyncSession, organization_id: int, freed: set[tuple[int, date]],
) -> list[dict]:
    """freed: (мастер, день), где только что освободилось время. Вызывать после изменения записи, до коммита:
    заявки помечаются уведомлёнными в той же транзакции. Возвращает письма для dispatch_waitlist_emails."""
    if not freed:
        return []
    with db.no_autoflush:
        organization = await db.get(models.Organization, organization_id)
        if organization is None or not get_subscription(organization).booking_enabled:
            return []
    await db.flush()  # освобождённый слот должен быть виден проверке свободного времени
    now = await get_org_now(db, organization_id)
    emails = []
    for master_id, day in freed:
        if day < now.date():
            continue
        entries = (await db.execute(
            select(models.WaitlistEntry).where(
                models.WaitlistEntry.organization_id == organization_id,
                models.WaitlistEntry.day == day,
                models.WaitlistEntry.notified_at.is_(None),
                or_(models.WaitlistEntry.master_id.is_(None), models.WaitlistEntry.master_id == master_id),
            ).order_by(models.WaitlistEntry.id),
        )).scalars().all()
        for entry in entries:
            service = await db.get(models.Service, entry.service_id)
            if service is None or service.deleted_at is not None:
                continue
            if not await _has_free_time(db, entry, service, now):
                continue
            entry.notified_at = utc_now()
            emails.append(dict(
                to_email=entry.email, organization_name=organization.name, organization_slug=organization.slug,
                service_name=service.name, service_id=service.id, day=entry.day, master_id=entry.master_id,
                language=entry.language,
            ))
    return emails


def dispatch_waitlist_emails(background_tasks: BackgroundTasks, emails: list[dict]) -> None:
    """Поставить письма в очередь (после коммита)."""
    for kwargs in emails:
        background_tasks.add_task(
            safe_send, send_waitlist_slot_free_email,
            log=("Failed to send waitlist email (%s)", kwargs["day"]), **kwargs,
        )


def freed_slot(appointment: models.Appointment) -> set[tuple[int, date]]:
    return {(appointment.master_id, appointment.start_time.date())}
