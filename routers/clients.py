from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from auth.auth import CurrentMembership, require_role
from common import get_owned
from db.database import get_db
from schemas.schemas import AppointmentWithDetails, ClientCreate, ClientPublic, ClientUpdate

router = APIRouter()


@router.post("", response_model=ClientPublic, status_code=status.HTTP_201_CREATED)
async def create_client(
    client: ClientCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    new_client = models.Client(
        organization_id=membership.organization_id,
        **client.model_dump(),
    )
    db.add(new_client)
    await db.commit()
    await db.refresh(new_client)
    return new_client


@router.get("", response_model=list[ClientPublic])
async def list_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Client).where(models.Client.organization_id == membership.organization_id),
    )
    return result.scalars().all()


@router.get("/{client_id}", response_model=ClientPublic)
async def get_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    return await get_owned(db, models.Client, client_id, membership.organization_id, "Client")


@router.patch("/{client_id}", response_model=ClientPublic)
async def update_client(
    client_id: int,
    client_update: ClientUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    client = await get_owned(db, models.Client, client_id, membership.organization_id, "Client")

    update_data = client_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(client, field, value)

    await db.commit()
    await db.refresh(client)
    return client


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership,
    Depends(require_role(models.MembershipRole.owner,
                         models.MembershipRole.admin))],
):
    client = await get_owned(db, models.Client, client_id, membership.organization_id, "Client")
    await db.delete(client)
    await db.commit()


@router.get("/{client_id}/appointments", response_model=list[AppointmentWithDetails])
async def get_client_appointments(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    status_filter: str | None = None,
):
    await get_owned(db, models.Client, client_id, membership.organization_id, "Client")

    query = (
        select(models.Appointment)
        .options(
            selectinload(models.Appointment.client),
            selectinload(models.Appointment.service),
            selectinload(models.Appointment.master),
        )
        .where(
            models.Appointment.client_id == client_id,
            models.Appointment.organization_id == membership.organization_id,
        )
    )
    if status_filter is not None:
        query = query.where(models.Appointment.status == status_filter)

    query = query.order_by(models.Appointment.start_time.desc())
    result = await db.execute(query)
    return result.scalars().all()