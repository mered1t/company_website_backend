from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from auth.auth import CurrentMembership, require_role, CurrentUser
from db.database import get_db
from schemas.schemas import (MasterCreate,
                             MasterPublic,
                             MasterUpdate,
                             WorkingHoursBase,
                             TimeOffCreate,
                             TimeOffPublic,
                             ConflictWarning,
                             WorkingHoursExceptionCreate,
                             WorkingHoursExceptionPublic)

from datetime import datetime as dt
from common import (get_owned,
                    log_activity,
                    check_no_active_appointments,
                    restore_entity,
                    check_no_history,
                    find_conflicting_appointments, get_owned_active)

from datetime import datetime as full_dt, time

router = APIRouter()


@router.post("", response_model=MasterPublic, status_code=status.HTTP_201_CREATED)
async def create_master(
    master: MasterCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    new_master = models.Master(
        organization_id=membership.organization_id,
        full_name=master.full_name,
        phone=master.phone,
        photo=master.photo,
    )
    for wh in master.working_hours:
        new_master.working_hours.append(
            models.WorkingHours(
                day_of_week=wh.day_of_week,
                start_time=wh.start_time,
                end_time=wh.end_time,
            ),
        )

    db.add(new_master)
    await db.flush()

    if master.service_ids:
        services_result = await db.execute(
            select(models.Service).where(
                models.Service.id.in_(master.service_ids),
                models.Service.organization_id == membership.organization_id,
                models.Service.deleted_at.is_(None),
            ),
        )
        for service in services_result.scalars().all():
            db.add(models.MasterService(master_id=new_master.id, service_id=service.id))
        await db.flush()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="master", entity_id=new_master.id,
        details=f"Created master {new_master.full_name}",
    )

    await db.commit()
    await db.refresh(new_master, attribute_names=["working_hours", "services"])
    return new_master


@router.get("", response_model=list[MasterPublic])
async def list_masters(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours), selectinload(models.Master.services))
        .where(
            models.Master.organization_id == membership.organization_id,
            models.Master.deleted_at.is_(None),
        )
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.get("/{master_id}", response_model=MasterPublic)
async def get_master(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours), selectinload(models.Master.services))
        .where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
            models.Master.deleted_at.is_(None),
        ),
    )
    master = result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    return master


@router.patch("/{master_id}", response_model=MasterPublic)
async def update_master(
    master_id: int,
    master_update: MasterUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours), selectinload(models.Master.services))
        .where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
            models.Master.deleted_at.is_(None),
        ),
    )
    master = result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    update_data = master_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(master, field, value)

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="updated", entity_type="master", entity_id=master_id,
        details=f"Updated fields: {', '.join(update_data.keys())}",
    )

    await db.commit()
    await db.refresh(master, attribute_names=["working_hours"])
    return master


@router.put("/{master_id}/working-hours", response_model=MasterPublic)
async def replace_working_hours(
    master_id: int,
    working_hours: list[WorkingHoursBase],
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership,
    Depends(require_role(models.MembershipRole.owner,
                         models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours), selectinload(models.Master.services))
        .where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
            models.Master.deleted_at.is_(None),
        ),
    )
    master = result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    master.working_hours.clear()
    for wh in working_hours:
        master.working_hours.append(
            models.WorkingHours(
                day_of_week=wh.day_of_week,
                start_time=wh.start_time,
                end_time=wh.end_time,
            ),
        )

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="updated", entity_type="master", entity_id=master_id,
        details=f"Updated working hours for {master.full_name}",
    )

    await db.commit()
    await db.refresh(master, attribute_names=["working_hours"])
    return master


@router.post("/{master_id}/time-off", response_model=TimeOffPublic | ConflictWarning, status_code=status.HTTP_201_CREATED)
async def create_time_off(
    master_id: int,
    time_off: TimeOffCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    await get_owned_active(db, models.Master, master_id, membership.organization_id, "Master")

    start_dt = full_dt.combine(time_off.start_date, time(0, 0))
    end_dt = full_dt.combine(time_off.end_date, time(23, 59, 59))

    conflicts = await find_conflicting_appointments(db, master_id, start_dt, end_dt)

    new_time_off = models.TimeOff(
        master_id=master_id,
        start_date=start_dt,
        end_date=end_dt,
        reason=time_off.reason,
    )
    db.add(new_time_off)
    await db.flush()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="time_off", entity_id=new_time_off.id,
        details=f"Time off from {time_off.start_date} to {time_off.end_date}",
    )

    await db.commit()

    if conflicts:
        return ConflictWarning(conflicting_appointment_ids=conflicts)

    await db.refresh(new_time_off)
    return TimeOffPublic(
        id=new_time_off.id,
        start_date=new_time_off.start_date.date(),
        end_date=new_time_off.end_date.date(),
        reason=new_time_off.reason,
    )


@router.get("/{master_id}/time-off", response_model=list[TimeOffPublic])
async def list_time_off(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    await get_owned_active(db, models.Master, master_id, membership.organization_id, "Master")

    result = await db.execute(
        select(models.TimeOff).where(models.TimeOff.master_id == master_id).order_by(models.TimeOff.start_date),
    )
    time_offs = result.scalars().all()
    return [
        TimeOffPublic(id=t.id, start_date=t.start_date.date(), end_date=t.end_date.date(), reason=t.reason)
        for t in time_offs
    ]


@router.delete("/{master_id}/time-off/{time_off_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_time_off(
    master_id: int,
    time_off_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.TimeOff).where(models.TimeOff.id == time_off_id, models.TimeOff.master_id == master_id),
    )
    time_off_obj = result.scalars().first()
    if not time_off_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Time off not found")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="time_off", entity_id=time_off_id,
    )

    await db.delete(time_off_obj)
    await db.commit()


@router.post("/{master_id}/schedule-exceptions", response_model=WorkingHoursExceptionPublic | ConflictWarning, status_code=status.HTTP_201_CREATED)
async def create_schedule_exception(
    master_id: int,
    exception: WorkingHoursExceptionCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    await get_owned_active(db, models.Master, master_id, membership.organization_id, "Master")

    exception_date = full_dt.combine(exception.date, time(0, 0))
    start_dt = full_dt.combine(exception.date, full_dt.strptime(exception.start_time, "%H:%M").time())
    end_dt = full_dt.combine(exception.date, full_dt.strptime(exception.end_time, "%H:%M").time())

    day_start = full_dt.combine(exception.date, time(0, 0))
    day_end = full_dt.combine(exception.date, time(23, 59, 59))
    all_day_conflicts = await find_conflicting_appointments(db, master_id, day_start, day_end)

    outside_new_hours = [
        appt_id for appt_id in all_day_conflicts
    ]

    new_exception = models.WorkingHoursException(
        master_id=master_id,
        date=exception_date,
        start_time=exception.start_time,
        end_time=exception.end_time,
    )
    db.add(new_exception)
    await db.flush()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="schedule_exception", entity_id=new_exception.id,
        details=f"Schedule exception on {exception.date}: {exception.start_time}-{exception.end_time}",
    )

    await db.commit()

    if outside_new_hours:
        return ConflictWarning(conflicting_appointment_ids=outside_new_hours)

    await db.refresh(new_exception)
    return new_exception


@router.get("/{master_id}/schedule-exceptions", response_model=list[WorkingHoursExceptionPublic])
async def list_schedule_exceptions(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    await get_owned_active(db, models.Master, master_id, membership.organization_id, "Master")

    result = await db.execute(
        select(models.WorkingHoursException)
        .where(models.WorkingHoursException.master_id == master_id)
        .order_by(models.WorkingHoursException.date),
    )
    return result.scalars().all()


@router.delete("/{master_id}/schedule-exceptions/{exception_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule_exception(
    master_id: int,
    exception_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.WorkingHoursException).where(
            models.WorkingHoursException.id == exception_id,
            models.WorkingHoursException.master_id == master_id,
        ),
    )
    exception_obj = result.scalars().first()
    if not exception_obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Schedule exception not found")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="schedule_exception", entity_id=exception_id,
    )

    await db.delete(exception_obj)
    await db.commit()


@router.post("/{master_id}/restore", response_model=MasterPublic)
async def restore_master(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    master = await restore_entity(db, models.Master, master_id, membership.organization_id, "Master")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="restored", entity_type="master", entity_id=master_id,
        details=f"Restored master {master.full_name}",
    )

    await db.commit()
    await db.refresh(master, attribute_names=["working_hours"])
    return master


@router.delete("/{master_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_master(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Master).where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
        ),
    )
    master = result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    await check_no_active_appointments(db, "master_id", master_id, "master")

    master.deleted_at = dt.now()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="master", entity_id=master_id,
        details=f"Deleted master {master.full_name}",
    )

    await db.commit()


@router.delete("/{master_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def hard_delete_master(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner))],
):
    result = await db.execute(
        select(models.Master).where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
        ),
    )
    master = result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")
    if master.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Master must be soft-deleted first")

    await check_no_history(db, "master_id", master_id, "master")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="hard_deleted", entity_type="master", entity_id=master_id,
        details=f"Permanently deleted master {master.full_name}",
    )

    await db.delete(master)
    await db.commit()