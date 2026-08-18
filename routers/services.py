from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser
from db.database import get_db
from schemas.schemas import ServiceCreate, ServicePublic, ServiceUpdate

router = APIRouter()


@router.post("", response_model=ServicePublic, status_code=status.HTTP_201_CREATED)
async def create_service(
    service: ServiceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    new_service = models.Service(
        owner_id=current_user.id,
        **service.model_dump(),
    )
    db.add(new_service)
    await db.commit()
    await db.refresh(new_service)
    return new_service


@router.get("", response_model=list[ServicePublic])
async def list_services(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Service).where(models.Service.owner_id == current_user.id),
    )
    return result.scalars().all()


@router.get("/{service_id}", response_model=ServicePublic)
async def get_service(
    service_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Service).where(
            models.Service.id == service_id,
            models.Service.owner_id == current_user.id,
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
):
    result = await db.execute(
        select(models.Service).where(
            models.Service.id == service_id,
            models.Service.owner_id == current_user.id,
        ),
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

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
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Service).where(
            models.Service.id == service_id,
            models.Service.owner_id == current_user.id,
        ),
    )
    service = result.scalars().first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Service not found")

    await db.delete(service)
    await db.commit()