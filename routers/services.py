from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentMembership, require_role, CurrentUser
from db.database import get_db
from schemas.schemas import ServiceCreate, ServicePublic, ServiceUpdate

from common import get_owned, log_activity
from datetime import datetime as dt

router = APIRouter()


@router.post("", response_model=ServicePublic, status_code=status.HTTP_201_CREATED)
async def create_service(
    service: ServiceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    new_service = models.Service(
        organization_id=membership.organization_id,
        **service.model_dump(),
    )
    db.add(new_service)
    await db.flush()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="service", entity_id=new_service.id,
        details=f"Created service {new_service.name}",
    )

    await db.commit()
    await db.refresh(new_service)
    return new_service


@router.get("", response_model=list[ServicePublic])
async def list_services(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    result = await db.execute(
        select(models.Service)
        .where(
            models.Service.organization_id == membership.organization_id,
            models.Service.deleted_at.is_(None),
        )
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.get("/{service_id}", response_model=ServicePublic)
async def get_service(
    service_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Service).where(
            models.Service.id == service_id,
            models.Service.organization_id == membership.organization_id,
            models.Service.deleted_at.is_(None),
        ),
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")
    return service


@router.patch("/{service_id}", response_model=ServicePublic)
async def update_service(
    service_id: int,
    service_update: ServiceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    service = await get_owned(db, models.Service, service_id, membership.organization_id, "Service")

    update_data = service_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(service, field, value)

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="updated", entity_type="service", entity_id=service_id,
        details=f"Updated fields: {', '.join(update_data.keys())}",
    )

    await db.commit()
    await db.refresh(service)
    return service


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    service = await get_owned(db, models.Service, service_id, membership.organization_id, "Service")
    service.deleted_at = dt.now()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="service", entity_id=service_id,
        details=f"Deleted service {service.name}",
    )

    await db.commit()