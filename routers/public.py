from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

import models
from db.database import get_db
from routers.appointments import _check_working_hours, _check_overlap
from schemas.schemas import (ServicePublic,
                             MasterPublic,
                             AvailableSlot,
                             PublicBookingRequest,
                             AppointmentPublic)


from datetime import date as date_type, datetime, timedelta
from datetime import datetime as dt
from rate_limiter import limiter

router = APIRouter()


async def get_organization_by_slug(slug: str, db: AsyncSession) -> models.Organization:
    result = await db.execute(select(models.Organization).where(models.Organization.slug == slug))
    org = result.scalars().first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


@router.get("/{slug}/services", response_model=list[ServicePublic])
async def public_list_services(slug: str, db: Annotated[AsyncSession, Depends(get_db)]):
    org = await get_organization_by_slug(slug, db)
    result = await db.execute(
        select(models.Service).where(
            models.Service.organization_id == org.id,
            models.Service.deleted_at.is_(None),
        ),
    )
    return result.scalars().all()


@router.get("/{slug}/masters", response_model=list[MasterPublic])
async def public_list_masters(slug: str, db: Annotated[AsyncSession, Depends(get_db)]):
    org = await get_organization_by_slug(slug, db)
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours))
        .where(
            models.Master.organization_id == org.id,
            models.Master.deleted_at.is_(None),
        ),
    )
    return result.scalars().all()


@router.get("/{slug}/available-slots", response_model=list[AvailableSlot])
async def get_available_slots(
    slug: str,
    master_id: int,
    service_id: int,
    date: date_type,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    org = await get_organization_by_slug(slug, db)

    service_result = await db.execute(
        select(models.Service).where(
            models.Service.id == service_id,
            models.Service.organization_id == org.id,
            models.Service.deleted_at.is_(None),
        ),
    )
    service = service_result.scalars().first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    master_result = await db.execute(
        select(models.Master).where(
            models.Master.id == master_id,
            models.Master.organization_id == org.id,
            models.Master.deleted_at.is_(None),
        ),
    )
    master = master_result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    day_of_week = date.weekday()
    wh_result = await db.execute(
        select(models.WorkingHours).where(
            models.WorkingHours.master_id == master_id,
            models.WorkingHours.day_of_week == day_of_week,
        ),
    )
    working_hours = wh_result.scalars().first()
    if not working_hours:
        return []

    day_start = datetime.combine(date, datetime.strptime(working_hours.start_time, "%H:%M").time())
    day_end = datetime.combine(date, datetime.strptime(working_hours.end_time, "%H:%M").time())

    appt_result = await db.execute(
        select(models.Appointment).where(
            models.Appointment.master_id == master_id,
            models.Appointment.status != "cancelled",
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time < day_end,
            models.Appointment.end_time > day_start,
        ),
    )
    existing_appointments = appt_result.scalars().all()

    step = timedelta(minutes=15)
    duration = timedelta(minutes=service.duration_minutes)

    slots = []
    current = day_start
    while current + duration <= day_end:
        slot_end = current + duration
        overlaps = any(
            current < appt.end_time and slot_end > appt.start_time
            for appt in existing_appointments
        )
        if not overlaps:
            slots.append(AvailableSlot(start_time=current, end_time=slot_end))
        current += step

    return slots


from datetime import datetime as dt

from routers.appointments import _check_working_hours, _check_overlap
from rate_limiter import limiter
from fastapi import Request


@router.post("/{slug}/book", response_model=AppointmentPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/hour")
async def public_create_booking(
    request: Request,
    slug: str,
    booking: PublicBookingRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    org = await get_organization_by_slug(slug, db)

    service_result = await db.execute(
        select(models.Service).where(
            models.Service.id == booking.service_id,
            models.Service.organization_id == org.id,
            models.Service.deleted_at.is_(None),
        ),
    )
    service = service_result.scalars().first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    master_result = await db.execute(
        select(models.Master).where(
            models.Master.id == booking.master_id,
            models.Master.organization_id == org.id,
            models.Master.deleted_at.is_(None),
        ),
    )
    master = master_result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    start_time = booking.start_time.replace(tzinfo=None)
    end_time = start_time + timedelta(minutes=service.duration_minutes)

    await _check_working_hours(db, booking.master_id, start_time, end_time)
    await _check_overlap(db, booking.master_id, start_time, end_time)

    client_result = await db.execute(
        select(models.Client).where(
            models.Client.organization_id == org.id,
            models.Client.phone == booking.client_phone,
            models.Client.deleted_at.is_(None),
        ),
    )
    client = client_result.scalars().first()

    if not client:
        client = models.Client(
            organization_id=org.id,
            full_name=booking.client_full_name,
            phone=booking.client_phone,
            email=booking.client_email,
        )
        db.add(client)
        await db.flush()
    elif client.email is None and booking.client_email is not None:
        client.email = booking.client_email

    active_count_result = await db.execute(
        select(models.Appointment).where(
            models.Appointment.client_id == client.id,
            models.Appointment.status == "scheduled",
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time > dt.now(),
        ),
    )
    active_appointments = active_count_result.scalars().all()
    if len(active_appointments) >= 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have 3 upcoming appointments. Please complete or cancel one before booking another.",
        )

    new_appointment = models.Appointment(
        organization_id=org.id,
        client_id=client.id,
        service_id=booking.service_id,
        master_id=booking.master_id,
        start_time=start_time,
        end_time=end_time,
        notes=booking.notes,
    )
    db.add(new_appointment)
    await db.commit()
    await db.refresh(new_appointment)
    return new_appointment