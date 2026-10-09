"""Уведомления («колокольчик») текущего пользователя в выбранной организации.

Подключён только к /api/v1. Фронтенд раз в 30-60 секунд спрашивает /unread-count, а список грузит при открытии.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentMembership
from core.time_utils import utc_now
from db.database import get_db
from schemas.notifications import NotificationPublic, ReadAllResult, UnreadCount

router = APIRouter()


def _mine(membership: models.Membership):
    """Условие «уведомление этого пользователя в этой организации»."""
    return (
        models.Notification.user_id == membership.user_id,
        models.Notification.organization_id == membership.organization_id,
    )


def _public(notification: models.Notification, client_name: str | None) -> NotificationPublic:
    return NotificationPublic(
        id=notification.id,
        type=notification.type,
        appointment_id=notification.appointment_id,
        client_name=client_name,
        service_name=notification.service_name,
        master_name=notification.master_name,
        start_time=notification.start_time,
        old_start_time=notification.old_start_time,
        created_at=notification.created_at,
        read_at=notification.read_at,
    )


@router.get("", response_model=list[NotificationPublic])
async def list_notifications(
    membership: CurrentMembership,
    db: Annotated[AsyncSession, Depends(get_db)],
    unread_only: bool = False,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
):
    """Свежие сверху."""
    query = (
        select(models.Notification, models.Client.full_name)
        .outerjoin(models.Client, models.Client.id == models.Notification.client_id)
        .where(*_mine(membership))
        .order_by(models.Notification.created_at.desc(), models.Notification.id.desc())
        .offset(skip)
        .limit(limit)
    )
    if unread_only:
        query = query.where(models.Notification.read_at.is_(None))
    return [_public(n, client_name) for n, client_name in (await db.execute(query)).all()]


@router.get("/unread-count", response_model=UnreadCount)
async def unread_count(membership: CurrentMembership, db: Annotated[AsyncSession, Depends(get_db)]):
    count = (await db.execute(
        select(func.count()).select_from(models.Notification)
        .where(*_mine(membership), models.Notification.read_at.is_(None)),
    )).scalar_one()
    return UnreadCount(unread=count)


@router.post("/read-all", response_model=ReadAllResult)
async def read_all(membership: CurrentMembership, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        update(models.Notification)
        .where(*_mine(membership), models.Notification.read_at.is_(None))
        .values(read_at=utc_now())
        .execution_options(synchronize_session=False),
    )
    await db.commit()
    return ReadAllResult(updated=result.rowcount)


@router.post("/{notification_id}/read", response_model=NotificationPublic)
async def read_one(
    notification_id: int, membership: CurrentMembership, db: Annotated[AsyncSession, Depends(get_db)],
):
    """Повторный вызов не ошибка (время прочтения не меняется)."""
    row = (await db.execute(
        select(models.Notification, models.Client.full_name)
        .outerjoin(models.Client, models.Client.id == models.Notification.client_id)
        .where(models.Notification.id == notification_id, *_mine(membership))
        .with_for_update(of=models.Notification),
    )).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found")
    notification, client_name = row
    if notification.read_at is None:
        notification.read_at = utc_now()
        await db.commit()
        await db.refresh(notification)
    return _public(notification, client_name)
