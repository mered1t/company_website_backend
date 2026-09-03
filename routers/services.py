from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentMembership, require_role
from common import get_owned
from db.database import get_db
from schemas.schemas import ServiceCreate, ServicePublic, ServiceUpdate

router = APIRouter()


@router.post("", response_model=ServicePublic, status_code=status.HTTP_201_CREATED)
async def create_service(
    service: ServiceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    new_service = models.Service(
        organization_id=membership.organization_id,
        **service.model_dump(),
    )
    db.add(new_service)
    await db.commit()
    await db.refresh(new_service)
    return new_service


@router.get("", response_model=list[ServicePublic])
async def list_services(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Service).where(models.Service.organization_id == membership.organization_id),
    )
    return result.scalars().all()


@router.get("/{service_id}", response_model=ServicePublic)
async def get_service(
    service_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    return await get_owned(db, models.Service, service_id, membership.organization_id, "Service")


@router.patch("/{service_id}", response_model=ServicePublic)
async def update_service(
    service_id: int,
    service_update: ServiceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership,
    Depends(require_role(
        models.MembershipRole.owner,
        models.MembershipRole.admin))],
):
    service = await get_owned(db, models.Service, service_id, membership.organization_id, "Service")

    update_data = service_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(service, field, value)

    await db.commit()
    await db.refresh(service)
    return service


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership,
    Depends(require_role(
        models.MembershipRole.owner,
        models.MembershipRole.admin))],
):
    service = await get_owned(db, models.Service, service_id, membership.organization_id, "Service")
    await db.delete(service)
    await db.commit()