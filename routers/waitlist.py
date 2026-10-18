"""Лист ожидания: публичная заявка «напишите, если освободится время» и список заявок в CRM."""
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import ManagerMembership
from core.rate_limiter import limiter
from db.database import get_db
from routers.public import get_organization_by_slug
from schemas.waitlist import WaitlistEntryPublic, WaitlistJoinRequest, WaitlistJoinResult
from services.booking import load_service_and_master, lock_client_phone
from services.common import check_booking_horizon, get_org_now, get_owned_active, set_total_count
from services.waitlist import MAX_ACTIVE_PER_PHONE

public_router = APIRouter()
router = APIRouter()


@public_router.post("/{slug}/waitlist", response_model=WaitlistJoinResult, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/hour")
async def join_waitlist(
    request: Request,
    slug: str,
    body: WaitlistJoinRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if body.website:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")
    org = await get_organization_by_slug(slug, db)
    if body.master_id is not None:
        await load_service_and_master(db, org.id, body.service_id, body.master_id)
    else:
        await get_owned_active(db, models.Service, body.service_id, org.id, "Service")

    today = (await get_org_now(db, org.id)).date()
    if body.day < today:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This day has already passed")
    await check_booking_horizon(db, org.id, body.day)

    await lock_client_phone(db, org.id, body.client_phone)
    active = (await db.execute(
        select(models.WaitlistEntry).where(
            models.WaitlistEntry.organization_id == org.id,
            models.WaitlistEntry.phone == body.client_phone,
            models.WaitlistEntry.notified_at.is_(None),
            models.WaitlistEntry.day >= today,
        ),
    )).scalars().all()
    if any(e.service_id == body.service_id and e.master_id == body.master_id and e.day == body.day for e in active):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You are already on the waiting list")
    if len(active) >= MAX_ACTIVE_PER_PHONE:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"At most {MAX_ACTIVE_PER_PHONE} waiting list requests per phone number",
        )

    entry = models.WaitlistEntry(
        organization_id=org.id, service_id=body.service_id, master_id=body.master_id, day=body.day,
        client_name=body.client_full_name, phone=body.client_phone, email=body.client_email,
        language=body.language or "en",
    )
    db.add(entry)
    await db.commit()
    return WaitlistJoinResult(id=entry.id, service_id=entry.service_id, master_id=entry.master_id, day=entry.day)


@router.get("", response_model=list[WaitlistEntryPublic])
async def list_waitlist(
    response: Response,
    membership: ManagerMembership,
    db: Annotated[AsyncSession, Depends(get_db)],
    day_from: date | None = None,
    day_to: date | None = None,
    include_notified: bool = False,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    """Заявки салона, ближайшие дни сначала. По умолчанию только те, кому письмо ещё не уходило."""
    query = select(models.WaitlistEntry).where(models.WaitlistEntry.organization_id == membership.organization_id)
    if day_from is not None:
        query = query.where(models.WaitlistEntry.day >= day_from)
    if day_to is not None:
        query = query.where(models.WaitlistEntry.day <= day_to)
    if not include_notified:
        query = query.where(models.WaitlistEntry.notified_at.is_(None))
    await set_total_count(response, db, query)
    rows = await db.execute(
        query.order_by(models.WaitlistEntry.day, models.WaitlistEntry.id).offset(skip).limit(limit),
    )
    return rows.scalars().all()


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_waitlist_entry(
    entry_id: int,
    membership: ManagerMembership,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    entry = await db.get(models.WaitlistEntry, entry_id)
    if entry is None or entry.organization_id != membership.organization_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Waiting list entry not found")
    await db.delete(entry)
    await db.commit()
