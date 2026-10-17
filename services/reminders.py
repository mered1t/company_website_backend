"""Напоминания о записи: за N часов до начала клиент получает письмо со ссылкой «отменить / перенести».

Кому: клиентам, записавшимся онлайн (email и язык из записи), и клиентам, которых записал сам салон в CRM,
если в карточке клиента есть email и у салона включена настройка staff_booking_reminders
(язык таких писем: client_email_language салона).

Как это работает:
  * берём записи со статусом scheduled, до начала которых осталось не больше reminder_hours_before часов
    (время считается по часовому поясу салона), у которых известен email клиента;
  * перед отправкой запись «занимается» одним атомарным UPDATE (reminded_start_time = start_time),
    поэтому два параллельных запуска (два сервиса, cron + фоновая задача) не пришлют письмо дважды;
  * если запись перенесли, start_time перестаёт совпадать с reminded_start_time, и напоминание уйдёт заново;
  * если клиент записался уже внутри окна (например, за 5 часов), письмо-подтверждение только что пришло,
    поэтому отдельное напоминание не отправляем;
  * если письмо не ушло (сбой Resend), отметка снимается, и следующая проверка попробует ещё раз.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from services.common import to_org_local
from core.config import settings
from services.email_service import send_booking_reminder_email
from domain.enums import AppointmentStatus
from services.booking_tokens import create_token, hash_token
from core.time_utils import utc_now
from services.billing import access_filter
from services.billing_notices import send_due_billing_notices
from services.notifications import delete_old_notifications
from services.cleanup import CLEANUP_INTERVAL, purge_old_rows

logger = logging.getLogger(__name__)

BATCH_LIMIT = 200  # за одну проверку; остальное подхватит следующая


@dataclass
class _Candidate:
    appointment_id: int
    start_time: datetime
    booked_inside_window: bool
    to_email: str | None
    language: str
    organization_name: str
    service_name: str
    master_name: str


async def _find_candidates(db: AsyncSession, window: timedelta) -> list[_Candidate]:
    # «сейчас» у каждого салона своё. Считаем его в Python отдельно для каждого часового пояса
    # (поясов мало), поэтому странное название пояса у одного салона не ломает напоминания у остальных
    tz_names = (await db.execute(select(models.Organization.timezone).distinct())).scalars().all()

    # у записи может быть несколько токенов (по одному на каждое напоминание): берём самый первый, он от записи
    first_token = (
        select(
            models.AppointmentToken.appointment_id.label("appointment_id"),
            func.min(models.AppointmentToken.id).label("token_id"),
        )
        .group_by(models.AppointmentToken.appointment_id)
        .subquery()
    )

    candidates: list[_Candidate] = []
    for tz_name in tz_names:
        try:
            org_now = to_org_local(utc_now(), tz_name)
        except ZoneInfoNotFoundError:
            logger.error("Booking reminders: unknown timezone %r, skipping its organizations", tz_name)
            continue

        result = await db.execute(
            select(models.Appointment, models.AppointmentToken)
            # у записей из CRM до первого напоминания токена нет: для них token будет None
            .outerjoin(first_token, first_token.c.appointment_id == models.Appointment.id)
            .outerjoin(models.AppointmentToken, models.AppointmentToken.id == first_token.c.token_id)
            .join(models.Organization, models.Organization.id == models.Appointment.organization_id)
            .join(models.Client, models.Client.id == models.Appointment.client_id)
            .options(
                selectinload(models.Appointment.service),
                selectinload(models.Appointment.master),
                selectinload(models.Appointment.client),
                selectinload(models.Appointment.organization),
            )
            .where(
                models.Organization.timezone == tz_name,
                access_filter(),  # закрытым организациям напоминания не шлём
                models.Appointment.status == AppointmentStatus.scheduled,
                models.Appointment.deleted_at.is_(None),
                models.Appointment.start_time > org_now,
                models.Appointment.start_time <= org_now + window,
                models.Appointment.reminded_start_time.is_distinct_from(models.Appointment.start_time),
                or_(
                    # онлайн-запись: email и язык клиент указал при записи
                    and_(models.Appointment.booked_online.is_(True), first_token.c.token_id.is_not(None)),
                    and_(  # запись из CRM: только если салон не отключил и есть куда писать
                        models.Appointment.booked_online.is_(False),
                        models.Organization.staff_booking_reminders.is_(True),
                        models.Client.email.is_not(None),
                        models.Client.email != "",
                        models.Client.deleted_at.is_(None),
                    ),
                ),
            )
            .order_by(models.Appointment.start_time)
            .limit(BATCH_LIMIT),
        )
        for appointment, token in result.all():
            # запись из CRM: подтверждения не было, напоминание нужно всегда. Токен у неё может уже быть
            # (от прошлого напоминания), но язык и адрес всё равно берём из настроек салона и карточки клиента
            if not appointment.booked_online:
                booked_inside_window = False
                to_email = appointment.client.email
                language = appointment.organization.client_email_language
            else:
                booked_inside_window = to_org_local(token.created_at, tz_name) > appointment.start_time - window
                to_email = token.email or appointment.client.email
                language = token.language
            candidates.append(_Candidate(
                appointment_id=appointment.id,
                start_time=appointment.start_time,
                booked_inside_window=booked_inside_window,
                to_email=to_email,
                language=language,
                organization_name=appointment.organization.name,
                service_name=appointment.service.name,
                master_name=appointment.master.full_name,
            ))
    return candidates


async def send_due_reminders(db: AsyncSession) -> int:
    """Отправляет все напоминания, которым пришло время. Возвращает, сколько писем ушло."""
    window = timedelta(hours=settings.reminder_hours_before)
    candidates = await _find_candidates(db, window)  # дальше в цикле есть commit: берём только готовые данные
    await db.rollback()  # закрываем транзакцию чтения

    sent = 0
    for c in candidates:
        if not c.booked_inside_window and not c.to_email:
            continue  # писать некому

        claim = await db.execute(
            update(models.Appointment)
            .where(
                models.Appointment.id == c.appointment_id,
                models.Appointment.status == AppointmentStatus.scheduled,
                models.Appointment.deleted_at.is_(None),
                models.Appointment.start_time == c.start_time,
                models.Appointment.reminded_start_time.is_distinct_from(c.start_time),
            )
            .values(reminded_start_time=c.start_time)
            .execution_options(synchronize_session=False),
        )
        if claim.rowcount == 0:  # запись изменилась или её уже взял другой запуск
            await db.rollback()
            continue

        if c.booked_inside_window:  # клиент только что получил подтверждение: просто закрываем напоминание
            await db.commit()
            continue

        manage_token = await create_token(db, c.appointment_id, c.language, c.to_email)
        await db.commit()

        try:
            await asyncio.to_thread(
                send_booking_reminder_email,
                to_email=c.to_email,
                organization_name=c.organization_name,
                service_name=c.service_name,
                master_name=c.master_name,
                start_time=c.start_time,
                manage_token=manage_token,
                language=c.language,
            )
        except Exception:
            logger.exception("Failed to send booking reminder (appointment_id=%s)", c.appointment_id)
            await db.execute(
                update(models.Appointment)
                .where(
                    models.Appointment.id == c.appointment_id,
                    models.Appointment.reminded_start_time == c.start_time,
                )
                .values(reminded_start_time=None)
                .execution_options(synchronize_session=False),
            )
            await db.execute(delete(models.AppointmentToken).where(
                models.AppointmentToken.token_hash == hash_token(manage_token),
            ))
            await db.commit()
            continue
        sent += 1
    return sent


async def reminder_loop(session_factory, interval_seconds: int, initial_delay: float = 10) -> None:
    """Бесконечный цикл для фоновой задачи внутри веб-сервиса. Любая ошибка пишется в лог, цикл не останавливается."""
    logger.info("Booking reminders: background check every %s s", interval_seconds)
    await asyncio.sleep(initial_delay)  # дать приложению спокойно подняться
    last_cleanup = None
    while True:
        try:
            async with session_factory() as db:
                count = await send_due_reminders(db)
            if count:
                logger.info("Sent %s booking reminder(s)", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Booking reminders check failed")
        # письма владельцам о конце пробного периода и подписки; сбой здесь не мешает напоминаниям и наоборот
        try:
            async with session_factory() as db:
                notices = await send_due_billing_notices(db)
            if notices:
                logger.info("Sent %s billing notice(s)", notices)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Billing notices check failed")
        # уведомления в CRM хранятся 30 дней
        try:
            async with session_factory() as db:
                removed = await delete_old_notifications(db)
            if removed:
                logger.info("Removed %s old notification(s)", removed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Notifications cleanup failed")
        # чистка устаревших строк (токены, старые приглашения, журнал) — раз в сутки
        if last_cleanup is None or utc_now() - last_cleanup >= CLEANUP_INTERVAL:
            last_cleanup = utc_now()
            try:
                async with session_factory() as db:
                    removed = await purge_old_rows(db)
                if any(removed.values()):
                    logger.info("Database cleanup: %s", {k: v for k, v in removed.items() if v})
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Database cleanup failed")
        await asyncio.sleep(interval_seconds)
