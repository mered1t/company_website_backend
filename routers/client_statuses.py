"""Справочник статусов клиентов салона: список, добавить, переименовать, перекрасить, удалить, поменять порядок.

Смотреть может любой сотрудник (статусы нужны в карточке клиента и фильтрах), менять справочник — владелец и админ.
Ставит статус клиенту любой сотрудник: PATCH /clients/{id} или POST /clients/bulk-status.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentMembership, ManagerMembership
from db.database import get_db
from schemas.schemas import ClientStatusCreate, ClientStatusOrder, ClientStatusPublic, ClientStatusUpdate
from services.client_statuses import MAX_STATUSES
from services.common import changed_fields_details, log_activity, only_changed

router = APIRouter()


async def _statuses(db: AsyncSession, organization_id: int) -> list[models.ClientStatus]:
    return list((await db.execute(
        select(models.ClientStatus)
        .where(models.ClientStatus.organization_id == organization_id)
        .order_by(models.ClientStatus.position, models.ClientStatus.id),
    )).scalars().all())


async def _owned(db: AsyncSession, status_id: int, organization_id: int) -> models.ClientStatus:
    row = await db.get(models.ClientStatus, status_id)
    if row is None or row.organization_id != organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client status not found")
    return row


async def _check_name_free(db: AsyncSession, organization_id: int, name: str, exclude_id: int | None = None):
    query = select(models.ClientStatus.id).where(
        models.ClientStatus.organization_id == organization_id,
        func.lower(models.ClientStatus.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.where(models.ClientStatus.id != exclude_id)
    if (await db.execute(query)).first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A status with this name already exists")


async def _public(db: AsyncSession, organization_id: int, rows: list[models.ClientStatus]) -> list[ClientStatusPublic]:
    counts = dict((await db.execute(
        select(models.Client.status_id, func.count(models.Client.id))
        .where(models.Client.organization_id == organization_id, models.Client.deleted_at.is_(None),
               models.Client.status_id.is_not(None))
        .group_by(models.Client.status_id),
    )).all())
    return [ClientStatusPublic(id=r.id, name=r.name, color=r.color, position=r.position,
                               clients_count=counts.get(r.id, 0)) for r in rows]


@router.get("", response_model=list[ClientStatusPublic])
async def list_client_statuses(db: Annotated[AsyncSession, Depends(get_db)], membership: CurrentMembership):
    """Статусы салона по порядку, с числом клиентов у каждого."""
    return await _public(db, membership.organization_id, await _statuses(db, membership.organization_id))


@router.post("", response_model=ClientStatusPublic, status_code=status.HTTP_201_CREATED)
async def create_client_status(
    body: ClientStatusCreate, db: Annotated[AsyncSession, Depends(get_db)], membership: ManagerMembership,
):
    org_id = membership.organization_id
    rows = await _statuses(db, org_id)
    if len(rows) >= MAX_STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=f"An organization can have at most {MAX_STATUSES} client statuses")
    await _check_name_free(db, org_id, body.name)
    row = models.ClientStatus(organization_id=org_id, name=body.name, color=body.color,
                              position=max((r.position for r in rows), default=-1) + 1)
    db.add(row)
    await db.flush()
    await log_activity(db, org_id, membership.user_id, action="created", entity_type="client_status",
                       entity_id=row.id, details=f"Created client status {row.name}")
    await db.commit()
    return (await _public(db, org_id, [row]))[0]


@router.patch("/{status_id}", response_model=ClientStatusPublic)
async def update_client_status(
    status_id: int, body: ClientStatusUpdate, db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
):
    org_id = membership.organization_id
    row = await _owned(db, status_id, org_id)
    data = only_changed(row, body.model_dump(exclude_unset=True))
    if "name" in data:
        await _check_name_free(db, org_id, data["name"], exclude_id=row.id)
    for field, value in data.items():
        setattr(row, field, value)
    if data:
        await log_activity(db, org_id, membership.user_id, action="updated", entity_type="client_status",
                           entity_id=row.id, details=changed_fields_details(data))
    await db.commit()
    return (await _public(db, org_id, [row]))[0]


@router.delete("/{status_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client_status(
    status_id: int, db: Annotated[AsyncSession, Depends(get_db)], membership: ManagerMembership,
):
    """Удаляет статус. У клиентов с этим статусом статус снимается, сами клиенты не меняются."""
    org_id = membership.organization_id
    row = await _owned(db, status_id, org_id)
    await db.execute(
        update(models.Client).where(models.Client.status_id == row.id).values(status_id=None)
        .execution_options(synchronize_session=False),
    )
    await log_activity(db, org_id, membership.user_id, action="deleted", entity_type="client_status",
                       entity_id=row.id, details=f"Deleted client status {row.name}")
    await db.delete(row)
    await db.commit()


@router.put("/order", response_model=list[ClientStatusPublic])
async def reorder_client_statuses(
    body: ClientStatusOrder, db: Annotated[AsyncSession, Depends(get_db)], membership: ManagerMembership,
):
    """Новый порядок: ids — все статусы салона, каждый ровно один раз."""
    org_id = membership.organization_id
    rows = await _statuses(db, org_id)
    if sorted(body.ids) != sorted(r.id for r in rows):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="ids must list every status of this organization exactly once")
    by_id = {r.id: r for r in rows}
    for position, status_id in enumerate(body.ids):
        by_id[status_id].position = position
    await db.commit()
    return await _public(db, org_id, await _statuses(db, org_id))
