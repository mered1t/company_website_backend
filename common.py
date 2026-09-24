from fastapi import HTTPException, status

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import secrets
import re
import models

from datetime import datetime, UTC, date, timedelta


async def get_owned(db: AsyncSession, model, obj_id: int, organization_id: int, name: str):
    result = await db.execute(
        select(model).where(model.id == obj_id, model.organization_id == organization_id),
    )
    obj = result.scalars().first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} not found")
    return obj


async def get_owned_active(db: AsyncSession, model, obj_id: int, organization_id: int, name: str):
    result = await db.execute(
        select(model).where(
            model.id == obj_id,
            model.organization_id == organization_id,
            model.deleted_at.is_(None),
        ),
    )
    obj = result.scalars().first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} not found")
    return obj


def slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-") or "salon"


async def generate_unique_slug(db: AsyncSession, model, base_text: str) -> str:
    base_slug = slugify(base_text)
    slug = base_slug
    counter = 1
    while True:
        result = await db.execute(select(model).where(model.slug == slug))
        if not result.scalars().first():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1



def generate_invitation_token() -> str:
    return secrets.token_urlsafe(32)


async def log_activity(
    db: AsyncSession,
    organization_id: int,
    user_id: int | None,
    action: str,
    entity_type: str,
    entity_id: int,
    details: str | None = None,
) -> None:
    log_entry = models.ActivityLog(
        organization_id=organization_id,
        user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=details,
    )
    db.add(log_entry)


async def check_no_active_appointments(db: AsyncSession, field_name: str, entity_id: int, entity_label: str) -> None:
    field = getattr(models.Appointment, field_name)
    result = await db.execute(
        select(models.Appointment).where(
            field == entity_id,
            models.Appointment.status == "scheduled",
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time > datetime.now(UTC).replace(tzinfo=None),
        ),
    )
    if result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete a {entity_label} with upcoming appointments. Cancel them first.",
        )


async def restore_entity(db: AsyncSession, model, obj_id: int, organization_id: int, name: str):
    result = await db.execute(
        select(model).where(
            model.id == obj_id,
            model.organization_id == organization_id,
        ),
    )
    obj = result.scalars().first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} not found")
    if obj.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"{name} is not deleted")
    obj.deleted_at = None
    return obj


async def check_no_history(db: AsyncSession, field_name: str, entity_id: int, entity_label: str) -> None:
    field = getattr(models.Appointment, field_name)
    result = await db.execute(select(models.Appointment).where(field == entity_id))
    if result.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot permanently delete a {entity_label} with appointment history",
        )


async def find_conflicting_appointments(db: AsyncSession, master_id: int, start_dt, end_dt) -> list[int]:
    result = await db.execute(
        select(models.Appointment.id).where(
            models.Appointment.master_id == master_id,
            models.Appointment.status == "scheduled",
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time < end_dt,
            models.Appointment.end_time > start_dt,
        ),
    )
    return [row[0] for row in result.all()]


async def get_available_intervals(db: AsyncSession, master_id: int, target_date) -> list[tuple[str, str]]:
    day_start = datetime.combine(target_date, datetime.min.time())
    day_end = datetime.combine(target_date, datetime.max.time().replace(microsecond=0))

    time_off_result = await db.execute(
        select(models.TimeOff).where(
            models.TimeOff.master_id == master_id,
            models.TimeOff.start_date <= day_end,
            models.TimeOff.end_date >= day_start,
        ),
    )
    if time_off_result.scalars().first():
        return []

    exceptions_result = await db.execute(
        select(models.WorkingHoursException).where(
            models.WorkingHoursException.master_id == master_id,
            models.WorkingHoursException.date >= day_start,
            models.WorkingHoursException.date <= day_end,
        ),
    )
    exceptions = exceptions_result.scalars().all()
    if exceptions:
        return [(e.start_time, e.end_time) for e in exceptions]

    day_of_week = target_date.weekday()
    wh_result = await db.execute(
        select(models.WorkingHours).where(
            models.WorkingHours.master_id == master_id,
            models.WorkingHours.day_of_week == day_of_week,
        ),
    )
    working_hours = wh_result.scalars().all()
    return [(wh.start_time, wh.end_time) for wh in working_hours]


async def check_booking_horizon(db: AsyncSession, organization_id: int, target_date) -> None:
    org_result = await db.execute(select(models.Organization).where(models.Organization.id == organization_id))
    org = org_result.scalars().first()
    max_date = date.today() + timedelta(days=org.booking_horizon_days)
    if target_date > max_date:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot book more than {org.booking_horizon_days} days in advance",
        )