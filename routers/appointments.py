from datetime import timedelta
from datetime import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

import models
from common import get_owned
from auth.auth import CurrentMembership, require_role
from db.database import get_db
from schemas.schemas import AppointmentCreate, AppointmentPublic, AppointmentUpdate, AppointmentWithDetails

router = APIRouter()


async def _check_working_hours(db: AsyncSession, master_id: int, start_time, end_time):
    day_of_week = start_time.weekday()
    result = await db.execute(
        select(models.WorkingHours).where(
            models.WorkingHours.master_id == master_id,
            models.WorkingHours.day_of_week == day_of_week,
        ),
    )
    working_hours = result.scalars().first()
    if not working_hours:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master does not work on this day")

    start_str = start_time.strftime("%H:%M")
    end_str = end_time.strftime("%H:%M")
    if start_str < working_hours.start_time or end_str > working_hours.end_time:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Appointment time is outside master's working hours")


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
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master already has an appointment at this time")


def _check_can_modify(membership: models.Membership, appointment: models.Appointment):
    if membership.role == models.MembershipRole.master and appointment.master_id != membership.master_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only modify your own appointments")


@router.post("", response_model=AppointmentPublic, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    appointment: AppointmentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    master_id = appointment.master_id
    if membership.role == models.MembershipRole.master:
        if membership.master_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your membership is not linked to a master profile")
        master_id = membership.master_id

    await get_owned(db, models.Client, appointment.client_id, membership.organization_id, "Client")
    service = await get_owned(db, models.Service, appointment.service_id, membership.organization_id, "Service")
    await get_owned(db, models.Master, master_id, membership.organization_id, "Master")

    start_time = appointment.start_time.replace(tzinfo=None)
    end_time = start_time + timedelta(minutes=service.duration_minutes)

    await _check_working_hours(db, master_id, start_time, end_time)
    await _check_overlap(db, master_id, start_time, end_time)

    new_appointment = models.Appointment(
        organization_id=membership.organization_id,
        client_id=appointment.client_id,
        service_id=appointment.service_id,
        master_id=master_id,
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
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Appointment).where(models.Appointment.organization_id == membership.organization_id),
    )
    return result.scalars().all()


@router.get("/calendar", response_model=list[AppointmentWithDetails])
async def get_calendar(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    date_from: dt,
    date_to: dt,
    master_id: int | None = None,
):
    query = (
        select(models.Appointment)
        .options(
            selectinload(models.Appointment.client),
            selectinload(models.Appointment.service),
            selectinload(models.Appointment.master),
        )
        .where(
            models.Appointment.organization_id == membership.organization_id,
            models.Appointment.start_time >= date_from,
            models.Appointment.start_time <= date_to,
        )
    )
    if master_id is not None:
        query = query.where(models.Appointment.master_id == master_id)

    query = query.order_by(models.Appointment.start_time)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{appointment_id}", response_model=AppointmentPublic)
async def get_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    return await get_owned(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")


@router.patch("/{appointment_id}", response_model=AppointmentPublic)
async def update_appointment(
    appointment_id: int,
    appointment_update: AppointmentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    appointment = await get_owned(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")
    _check_can_modify(membership, appointment)

    update_data = appointment_update.model_dump(exclude_unset=True)
    recheck_needed = any(k in update_data for k in ("start_time", "master_id", "service_id"))

    for field, value in update_data.items():
        setattr(appointment, field, value)

    if appointment.start_time.tzinfo is not None:
        appointment.start_time = appointment.start_time.replace(tzinfo=None)

    if recheck_needed:
        service = await get_owned(db, models.Service, appointment.service_id, membership.organization_id, "Service")
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
    membership: CurrentMembership,
):
    appointment = await get_owned(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")
    if membership.role == models.MembershipRole.master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Masters cannot delete appointments")

    await db.delete(appointment)
    await db.commit()