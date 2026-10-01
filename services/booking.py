"""Единое место для правил бронирования.

Любая запись (админка, публичная страница) проходит через эти функции,
чтобы правила не расходились.
"""
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from common import get_available_intervals, get_org_currency
from enums import AppointmentStatus

OVERLAP_CONSTRAINT = "appointments_no_master_overlap"
MASTER_BUSY = "Master already has an appointment at this time"


def is_overlap_violation(error: IntegrityError) -> bool:
    return OVERLAP_CONSTRAINT in str(error.orig)


async def check_master_provides_service(db: AsyncSession, master_id: int, service_id: int) -> None:
    """Если у мастера задан список услуг, выбранная услуга должна быть в нём."""
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.services))
        .where(models.Master.id == master_id),
    )
    master = result.scalars().first()
    if master and master.services and service_id not in {s.id for s in master.services}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This master does not provide this service",
        )


async def check_working_hours(db: AsyncSession, master_id: int, start_time: datetime, end_time: datetime) -> None:
    intervals = await get_available_intervals(db, master_id, start_time.date())
    if not intervals:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Master does not work on this day")

    start_str = start_time.strftime("%H:%M")
    end_str = end_time.strftime("%H:%M")
    if not any(start_str >= s and end_str <= e for s, e in intervals):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Appointment time is outside master's working hours")


async def check_overlap(db: AsyncSession, master_id: int, start_time: datetime, end_time: datetime,
                        exclude_id: int | None = None) -> None:
    query = select(models.Appointment).where(
        models.Appointment.master_id == master_id,
        models.Appointment.status != AppointmentStatus.cancelled,
        models.Appointment.deleted_at.is_(None),
        models.Appointment.start_time < end_time,
        models.Appointment.end_time > start_time,
    )
    if exclude_id is not None:
        query = query.where(models.Appointment.id != exclude_id)

    result = await db.execute(query)
    if result.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MASTER_BUSY)


async def check_slot_free(db: AsyncSession, master_id: int, start_time: datetime, end_time: datetime,
                          exclude_id: int | None = None) -> None:
    """Рабочие часы + отсутствие пересечений.

    no_autoflush: проверки читают данные из базы и не должны отправлять туда
    наши ещё не сохранённые правки (иначе защита от пересечений сработает раньше,
    чем наша проверка, и вместо 400 получится 500).
    """
    with db.no_autoflush:
        await check_working_hours(db, master_id, start_time, end_time)
        await check_overlap(db, master_id, start_time, end_time, exclude_id=exclude_id)


async def persist(db: AsyncSession, *, commit: bool, detail: str = MASTER_BUSY) -> None:
    """flush или commit; нарушение защиты от двойной записи превращает в 400 вместо 500."""
    try:
        if commit:
            await db.commit()
        else:
            await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if is_overlap_violation(e):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        raise


async def create_appointment_record(
    db: AsyncSession,
    *,
    organization_id: int,
    client_id: int,
    service: models.Service,
    master_id: int,
    start_time: datetime,
    end_time: datetime,
    notes: str | None = None,
) -> models.Appointment:
    """Создаёт запись со снимком цены и валюты. Единственное место, где это делается."""
    currency = await get_org_currency(db, organization_id)
    appointment = models.Appointment(
        organization_id=organization_id,
        client_id=client_id,
        service_id=service.id,
        master_id=master_id,
        start_time=start_time,
        end_time=end_time,
        price=service.price,
        currency=currency,
        notes=notes,
    )
    db.add(appointment)
    await persist(db, commit=False)
    return appointment