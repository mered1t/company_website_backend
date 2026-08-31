from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from auth.auth import CurrentMembership
from common import get_owned
from db.database import get_db
from schemas.schemas import MasterCreate, MasterPublic, MasterUpdate, WorkingHoursBase

router = APIRouter()


@router.post("", response_model=MasterPublic, status_code=status.HTTP_201_CREATED)
async def create_master(
    master: MasterCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
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
    await db.commit()
    await db.refresh(new_master, attribute_names=["working_hours"])
    return new_master


@router.get("", response_model=list[MasterPublic])
async def list_masters(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours))
        .where(models.Master.organization_id == membership.organization_id),
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
        .options(selectinload(models.Master.working_hours))
        .where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
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
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours))
        .where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
        ),
    )
    master = result.scalars().first()
    if not master:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found")

    update_data = master_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(master, field, value)

    await db.commit()
    await db.refresh(master, attribute_names=["working_hours"])
    return master


@router.put("/{master_id}/working-hours", response_model=MasterPublic)
async def replace_working_hours(
    master_id: int,
    working_hours: list[WorkingHoursBase],
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours))
        .where(
            models.Master.id == master_id,
            models.Master.organization_id == membership.organization_id,
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

    await db.commit()
    await db.refresh(master, attribute_names=["working_hours"])
    return master


@router.delete("/{master_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_master(
    master_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
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

    await db.delete(master)
    await db.commit()