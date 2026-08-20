from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser
from db.database import get_db
from schemas.schemas import AppointmentCreate, AppointmentPublic, AppointmentUpdate

router = APIRouter()


async def _get_owned(db: AsyncSession, model, obj_id: int, owner_id: int, name: str):
    result = await db.execute(
        select(model).where(model.id == obj_id, model.owner_id == owner_id),
    )
    obj = result.scalars().first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} not found")
    return obj


async def _check_working_hours(db: AsyncSession, master_id: int, start_time, end_time):
    day_of_week = start_time.weekday()  # 0 = понедельник ... 6 = воскресенье
    result = await db.execute(
        select(models.WorkingHours).where(
            models.WorkingHours.master_id == master_id,
            models.WorkingHours.day_of_week == day_of_week,
        ),
    )
    working_hours = result.scalars().first()
    if not working_hours:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Master does not work on this day",
        )

    start_str = start_time.strftime("%H:%M")
    end_str = end_time.strftime("%H:%M")
    if start_str < working_hours.start_time or end_str > working_hours.end_time:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Appointment time is outside master's working hours",
        )


async def _check_overlap(db: AsyncSession, master_id: int, start_time, end_time, exclude_id: int | None = None):
    query = select(models.Appointment).where(
        models.Appointment.master_id == master_id,
        models.Appointment.status != "cancelled",
        models.Appointment.start_time < end_time,
        models.Appointment.end_time > start_time,
    )
    if exclude_id is not None:
        query = query.where(models.Appointment.id != exclude_id)

    result = await db.execute(query)
    if result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Master already has an appointment at this time",
        )


@router.post("", response_model=AppointmentPublic, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    appointment: AppointmentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    await _get_owned(db, models.Client, appointment.client_id, current_user.id, "Client")
    service = await _get_owned(db, models.Service, appointment.service_id, current_user.id, "Service")
    await _get_owned(db, models.Master, appointment.master_id, current_user.id, "Master")

    start_time = appointment.start_time
    end_time = start_time + timedelta(minutes=service.duration_minutes)

    await _check_working_hours(db, appointment.master_id, start_time, end_time)
    await _check_overlap(db, appointment.master_id, start_time, end_time)

    new_appointment = models.Appointment(
        owner_id=current_user.id,
        client_id=appointment.client_id,
        service_id=appointment.service_id,
        master_id=appointment.master_id,
        start_time=start_time,
        end_time=end_time,
        notes=appointment.notes,
    )
    db.add(new_appointment)
    await db.commit()
    await db.refresh(new_appointment)
    return new_appointment


@router.get("", response_model=list[AppointmentPublic])
async def list_appointments(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Appointment).where(models.Appointment.owner_id == current_user.id),
    )
    return result.scalars().all()


@router.get("/{appointment_id}", response_model=AppointmentPublic)
async def get_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    return await _get_owned(db, models.Appointment, appointment_id, current_user.id, "Appointment")


@router.patch("/{appointment_id}", response_model=AppointmentPublic)
async def update_appointment(
    appointment_id: int,
    appointment_update: AppointmentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    appointment = await _get_owned(db, models.Appointment, appointment_id, current_user.id, "Appointment")
    update_data = appointment_update.model_dump(exclude_unset=True)

    recheck_needed = any(k in update_data for k in ("start_time", "master_id", "service_id"))

    for field, value in update_data.items():
        setattr(appointment, field, value)

    if recheck_needed:
        service = await _get_owned(db, models.Service, appointment.service_id, current_user.id, "Service")
        appointment.end_time = appointment.start_time + timedelta(minutes=service.duration_minutes)
        await _check_working_hours(db, appointment.master_id, appointment.start_time, appointment.end_time)
        await _check_overlap(db, appointment.master_id, appointment.start_time, appointment.end_time, exclude_id=appointment.id)

    await db.commit()
    await db.refresh(appointment)
    return appointment


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    appointment = await _get_owned(db, models.Appointment, appointment_id, current_user.id, "Appointment")
    await db.delete(appointment)
    await db.commit()