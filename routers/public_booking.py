"""Управление записью по ссылке из письма: просмотр, свободные слоты, отмена, перенос.

Логина нет: доступ даёт секретный токен. Эндпоинты подключены к public-роутеру (/public/booking/...).
"""
from datetime import date as date_type, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

import models
from services.common import check_booking_horizon, get_org_now, log_activity
from core.config import settings
from db.database import get_db
from domain.enums import AppointmentStatus
from core.rate_limiter import limiter
from schemas.schemas import AvailableSlot, BookingManageInfo, BookingRescheduleRequest
from services.booking import check_slot_free, get_free_slots, persist
from services.booking_notifications import dispatch, prepare_cancelled_email, prepare_changed_email
from services.booking_tokens import load_by_token
from services.salon_notifications import dispatch_all, prepare_salon_cancelled, prepare_salon_rescheduled
from services.billing import ensure_booking_enabled

router = APIRouter()


def _cutoff() -> timedelta:
    return timedelta(hours=settings.booking_change_cutoff_hours)


def _info(appointment: models.Appointment, org_now: datetime) -> BookingManageInfo:
    deadline = appointment.start_time - _cutoff()
    return BookingManageInfo(
        appointment_id=appointment.id,
        status=appointment.status,
        start_time=appointment.start_time,
        end_time=appointment.end_time,
        service_id=appointment.service_id,
        service_name=appointment.service.name,
        master_id=appointment.master_id,
        master_name=appointment.master.full_name,
        price=appointment.price,
        currency=appointment.currency,
        organization_name=appointment.organization.name,
        organization_slug=appointment.organization.slug,
        can_modify=appointment.status == AppointmentStatus.scheduled and org_now < deadline,
        modify_deadline=deadline,
    )


def _ensure_modifiable(appointment: models.Appointment, org_now: datetime) -> None:
    if appointment.status != AppointmentStatus.scheduled:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This appointment can no longer be changed")
    if org_now >= appointment.start_time - _cutoff():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too late to change this appointment online (up to {settings.booking_change_cutoff_hours} h before the start). "
                   "Please contact the salon.",
        )


@router.get("/booking/{token}", response_model=BookingManageInfo)
@limiter.limit("30/minute")
async def get_booking(request: Request, token: str, db: Annotated[AsyncSession, Depends(get_db)]):
    appointment = await load_by_token(db, token)
    org_now = await get_org_now(db, appointment.organization_id)
    return _info(appointment, org_now)


@router.get("/booking/{token}/available-slots", response_model=list[AvailableSlot])
@limiter.limit("30/minute")
async def get_booking_slots(
    request: Request,
    token: str,
    date: date_type,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Свободное время у того же мастера на ту же длительность. Сама запись слот не занимает."""
    appointment = await load_by_token(db, token)
    org_now = await get_org_now(db, appointment.organization_id)
    ensure_booking_enabled(appointment.organization)  # у закрытого салона новое время выбрать нельзя
    _ensure_modifiable(appointment, org_now)
    await check_booking_horizon(db, appointment.organization_id, date)

    duration_minutes = int((appointment.end_time - appointment.start_time).total_seconds() // 60)
    slots = await get_free_slots(db, appointment.master_id, duration_minutes, date, org_now, exclude_id=appointment.id)
    return [AvailableSlot(start_time=s, end_time=e) for s, e in slots]


@router.post("/booking/{token}/cancel", response_model=BookingManageInfo)
@limiter.limit("10/hour")
async def cancel_booking(
    request: Request,
    token: str,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    appointment = await load_by_token(db, token)
    org_now = await get_org_now(db, appointment.organization_id)

    if appointment.status == AppointmentStatus.cancelled:
        return _info(appointment, org_now)  # повторное нажатие не ошибка

    _ensure_modifiable(appointment, org_now)
    appointment.status = AppointmentStatus.cancelled
    await log_activity(
        db, appointment.organization_id, None,
        action="updated", entity_type="appointment", entity_id=appointment.id,
        details="Cancelled by the client via the link from the email",
    )
    notification = await prepare_cancelled_email(db, appointment, by_salon=False)
    salon_notifications = await prepare_salon_cancelled(db, appointment)  # письма владельцу, админам и мастеру
    info = _info(appointment, org_now)  # собираем до commit: после него объекты могут устареть
    await db.commit()
    dispatch(background_tasks, notification)
    dispatch_all(background_tasks, salon_notifications)
    return info


@router.post("/booking/{token}/reschedule", response_model=BookingManageInfo)
@limiter.limit("10/hour")
async def reschedule_booking(
    request: Request,
    token: str,
    body: BookingRescheduleRequest,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Переносит ту же запись на новое время (отмены в статистике не появляется)."""
    appointment = await load_by_token(db, token)
    org_now = await get_org_now(db, appointment.organization_id)
    ensure_booking_enabled(appointment.organization)
    _ensure_modifiable(appointment, org_now)

    new_start = body.start_time.replace(tzinfo=None)
    if new_start <= org_now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot book a time in the past")
    await check_booking_horizon(db, appointment.organization_id, new_start.date())

    new_end = new_start + (appointment.end_time - appointment.start_time)
    await check_slot_free(db, appointment.master_id, new_start, new_end, exclude_id=appointment.id)

    old_start = appointment.start_time
    appointment.start_time = new_start
    appointment.end_time = new_end
    await log_activity(
        db, appointment.organization_id, None,
        action="updated", entity_type="appointment", entity_id=appointment.id,
        details=f"Rescheduled by the client via the link from the email: {old_start:%Y-%m-%d %H:%M} -> {new_start:%Y-%m-%d %H:%M}",
    )
    await persist(db, commit=False)  # защита от двойной записи превращается в 400
    notification = await prepare_changed_email(db, appointment, old_start=old_start, by_salon=False)
    salon_notifications = await prepare_salon_rescheduled(db, appointment, old_start=old_start)
    info = _info(appointment, org_now)
    await db.commit()
    dispatch(background_tasks, notification)
    dispatch_all(background_tasks, salon_notifications)
    return info
