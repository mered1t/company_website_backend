from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

import models
from auth.auth import CurrentMembership, require_role, CurrentUser
from db.database import get_db
from schemas.schemas import AppointmentWithDetails, ClientCreate, ClientPublic, ClientUpdate

from datetime import datetime as dt
from common import get_owned, log_activity

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
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A client with this phone number already exists")
    await db.refresh(new_client)
    return new_client


@router.get("", response_model=list[ClientPublic])
async def list_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    result = await db.execute(
        select(models.Client)
        .where(
            models.Client.organization_id == membership.organization_id,
            models.Client.deleted_at.is_(None),
        )
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.get("/{client_id}", response_model=ClientPublic)
async def get_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Client).where(
            models.Client.id == client_id,
            models.Client.organization_id == membership.organization_id,
            models.Client.deleted_at.is_(None),
        ),
    )
    client = result.scalars().first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


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

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A client with this phone number already exists")
    await db.refresh(client)
    return client


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    client = await get_owned(db, models.Client, client_id, membership.organization_id, "Client")
    client.deleted_at = dt.now()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="client", entity_id=client_id,
        details=f"Deleted client {client.full_name}",
    )

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