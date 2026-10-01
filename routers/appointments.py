from datetime import timedelta
from datetime import datetime as dt
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

import models
from auth.auth import CurrentMembership, require_role, CurrentUser
from db.database import get_db
from schemas.schemas import AppointmentCreate, AppointmentPublic, AppointmentUpdate, AppointmentWithDetails

from common import get_owned, get_owned_active, log_activity, restore_entity, get_available_intervals

router = APIRouter()


OVERLAP_CONSTRAINT = "appointments_no_master_overlap"
MASTER_BUSY = "Master already has an appointment at this time"


def _is_overlap_violation(error: IntegrityError) -> bool:
    return OVERLAP_CONSTRAINT in str(error.orig)


async def _check_working_hours(db: AsyncSession, master_id: int, start_time, end_time):
    intervals = await get_available_intervals(db, master_id, start_time.date())
    if not intervals:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Master does not work on this day")

    start_str = start_time.strftime("%H:%M")
    end_str = end_time.strftime("%H:%M")

    fits = any(start_str >= s and end_str <= e for s, e in intervals)
    if not fits:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Appointment time is outside master's working hours")


async def _check_overlap(db: AsyncSession, master_id: int, start_time, end_time, exclude_id: int | None = None):
    query = select(models.Appointment).where(
        models.Appointment.master_id == master_id,
        models.Appointment.status != "cancelled",
        models.Appointment.deleted_at.is_(None),
        models.Appointment.start_time < end_time,
        models.Appointment.end_time > start_time,
    )
    if exclude_id is not None:
        query = query.where(models.Appointment.id != exclude_id)

    result = await db.execute(query)
    if result.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MASTER_BUSY)


async def _check_master_provides_service(db: AsyncSession, master_id: int, service_id: int):
    result = await db.execute(
        select(models.MasterService.service_id).where(models.MasterService.master_id == master_id),
    )
    provided_ids = set(result.scalars().all())
    # пустой список услуг у мастера = делает все услуги (как в админской записи)
    if provided_ids and service_id not in provided_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This master does not provide this service",
        )


def _check_can_modify(membership: models.Membership, appointment: models.Appointment):
    if membership.role == models.MembershipRole.master and appointment.master_id != membership.master_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only modify your own appointments")


@router.post("", response_model=AppointmentPublic, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    appointment: AppointmentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    master_id = appointment.master_id
    if membership.role == models.MembershipRole.master:
        if membership.master_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your membership is not linked to a master profile")
        master_id = membership.master_id

    await get_owned_active(db, models.Client, appointment.client_id, membership.organization_id, "Client")
    service = await get_owned_active(db, models.Service, appointment.service_id, membership.organization_id, "Service")
    await get_owned_active(db, models.Master, master_id, membership.organization_id, "Master")

    master_services_result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.services))
        .where(models.Master.id == master_id),
    )
    master_obj = master_services_result.scalars().first()

    if master_obj.services and service not in master_obj.services:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This master does not provide this service",
        )

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
        price=service.price,
        notes=appointment.notes,
    )

    db.add(new_appointment)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if _is_overlap_violation(e):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MASTER_BUSY)
        raise

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="appointment", entity_id=new_appointment.id,
    )

    await db.commit()
    await db.refresh(new_appointment)
    return new_appointment


@router.get("", response_model=list[AppointmentPublic])
async def list_appointments(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    result = await db.execute(
        select(models.Appointment)
        .where(
            models.Appointment.organization_id == membership.organization_id,
            models.Appointment.deleted_at.is_(None),
        )
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.get("/calendar", response_model=list[AppointmentWithDetails])
async def get_calendar(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    date_from: dt,
    date_to: dt,
    master_id: int | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
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
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time >= date_from,
            models.Appointment.start_time <= date_to,
        )
    )
    if master_id is not None:
        query = query.where(models.Appointment.master_id == master_id)

    query = query.order_by(models.Appointment.start_time).offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{appointment_id}", response_model=AppointmentPublic)
async def get_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Appointment).where(
            models.Appointment.id == appointment_id,
            models.Appointment.organization_id == membership.organization_id,
            models.Appointment.deleted_at.is_(None),
        ),
    )
    appointment = result.scalars().first()
    if not appointment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    return appointment


@router.patch("/{appointment_id}", response_model=AppointmentPublic)
async def update_appointment(
    appointment_id: int,
    appointment_update: AppointmentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    appointment = await get_owned_active(db, models.Appointment, appointment_id,
                                         membership.organization_id, "Appointment")
    _check_can_modify(membership, appointment)

    update_data = appointment_update.model_dump(exclude_unset=True)

    # Проверяем, что переданные id принадлежат ЭТОЙ организации
    if "client_id" in update_data:
        await get_owned_active(db, models.Client, update_data["client_id"],
                               membership.organization_id, "Client")

    if "service_id" in update_data:
        await get_owned_active(db, models.Service, update_data["service_id"],
                               membership.organization_id, "Service")

    if "master_id" in update_data:
        if membership.role == models.MembershipRole.master:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Masters cannot reassign appointments to another master",
            )
        await get_owned_active(db, models.Master, update_data["master_id"],
                               membership.organization_id, "Master")

    recheck_needed = any(k in update_data for k in ("start_time", "master_id", "service_id"))

    for field, value in update_data.items():
        setattr(appointment, field, value)

    if appointment.start_time.tzinfo is not None:
        appointment.start_time = appointment.start_time.replace(tzinfo=None)

    try:
        if recheck_needed:
            service = await get_owned_active(db, models.Service, appointment.service_id,
                                             membership.organization_id, "Service")
            if "service_id" in update_data:
                appointment.price = service.price  # услугу сменили, значит и цена новая

            master_result = await db.execute(
                select(models.Master)
                .options(selectinload(models.Master.services))
                .where(models.Master.id == appointment.master_id),
            )
            master_obj = master_result.scalars().first()
            if master_obj.services and service not in master_obj.services:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="This master does not provide this service",
                )

            appointment.end_time = appointment.start_time + timedelta(minutes=service.duration_minutes)
            await _check_working_hours(db, appointment.master_id, appointment.start_time, appointment.end_time)
            await _check_overlap(db, appointment.master_id, appointment.start_time,
                                 appointment.end_time, exclude_id=appointment.id)

        await log_activity(
            db, membership.organization_id, current_user.id,
            action="updated", entity_type="appointment", entity_id=appointment_id,
            details=f"Updated fields: {', '.join(update_data.keys())}",
        )

        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        if _is_overlap_violation(e):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MASTER_BUSY)
        raise

    await db.refresh(appointment)
    return appointment


@router.post("/{appointment_id}/restore", response_model=AppointmentPublic)
async def restore_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    appointment = await restore_entity(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="restored", entity_type="appointment", entity_id=appointment_id,
        details="Restored appointment",
    )

    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        if _is_overlap_violation(e):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Cannot restore: the master already has an appointment at this time")
        raise
    await db.refresh(appointment)
    return appointment


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    appointment = await get_owned(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")
    if membership.role == models.MembershipRole.master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Masters cannot delete appointments")

    appointment.deleted_at = dt.now()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="appointment", entity_id=appointment_id,
    )

    await db.commit()


@router.delete("/{appointment_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def hard_delete_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner))],
):
    result = await db.execute(
        select(models.Appointment).where(
            models.Appointment.id == appointment_id,
            models.Appointment.organization_id == membership.organization_id,
        ),
    )
    appointment = result.scalars().first()
    if not appointment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    if appointment.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Appointment must be soft-deleted first")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="hard_deleted", entity_type="appointment", entity_id=appointment_id,
        details="Permanently deleted appointment",
    )

    await db.delete(appointment)
    await db.commit()