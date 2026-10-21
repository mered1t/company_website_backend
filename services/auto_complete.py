"""Автоотметка «Пришёл»: прошедшие записи, которые так и остались scheduled, переводятся в completed.

Мастера часто забывают отмечать визиты, и выручка в аналитике занижена. Настройка салона auto_complete:
  * end_of_day (по умолчанию) — после конца дня записи по поясу салона: днём мастер успевает отметить неявки;
  * after_2h — через 2 часа после конца записи;
  * off — выключено.
Берутся только записи последних LOOKBACK_DAYS дней: старые неотмеченные (например, до включения функции) не
трогаются, их салон разбирает сам. Отмеченные так записи получают auto_completed = true (значок в CRM);
неявку салон ставит обычным PATCH, флаг при этом снимается. Писем клиентам нет.
Запускается из фонового цикла напоминаний (services/reminders.py).
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.time_utils import utc_now
from domain.enums import AppointmentStatus
from services.billing import access_filter
from services.common import to_org_local

logger = logging.getLogger(__name__)

LOOKBACK_DAYS = 3
AFTER_HOURS = 2


async def auto_complete_appointments(db: AsyncSession) -> int:
    """Отмечает прошедшие записи выполненными. Возвращает, сколько записей отмечено."""
    tz_names = (await db.execute(select(models.Organization.timezone).distinct())).scalars().all()
    total = 0
    for tz_name in tz_names:
        try:
            org_now = to_org_local(utc_now(), tz_name)
        except ZoneInfoNotFoundError:
            continue
        today_start = datetime.combine(org_now.date(), datetime.min.time())
        for mode, ended_before in (("end_of_day", today_start), ("after_2h", org_now - timedelta(hours=AFTER_HOURS))):
            orgs = (
                select(models.Organization.id)
                .where(models.Organization.timezone == tz_name, models.Organization.auto_complete == mode,
                       access_filter())
            )
            result = await db.execute(
                update(models.Appointment)
                .where(
                    models.Appointment.organization_id.in_(orgs),
                    models.Appointment.status == AppointmentStatus.scheduled,
                    models.Appointment.deleted_at.is_(None),
                    models.Appointment.end_time <= ended_before,
                    models.Appointment.start_time >= today_start - timedelta(days=LOOKBACK_DAYS),
                )
                .values(status=AppointmentStatus.completed, auto_completed=True)
                .execution_options(synchronize_session=False),
            )
            total += result.rowcount
    await db.commit()
    return total
