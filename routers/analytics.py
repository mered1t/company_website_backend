from datetime import datetime as dt, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser
from db.database import get_db

router = APIRouter()


@router.get("/revenue")
async def get_revenue(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    date_from: dt,
    date_to: dt,
):
    result = await db.execute(
        select(func.coalesce(func.sum(models.Service.price), 0))
        .select_from(models.Appointment)
        .join(models.Service, models.Appointment.service_id == models.Service.id)
        .where(
            models.Appointment.owner_id == current_user.id,
            models.Appointment.status == "completed",
            models.Appointment.start_time >= date_from,
            models.Appointment.start_time <= date_to,
        ),
    )
    total = result.scalar()
    return {"date_from": date_from, "date_to": date_to, "total_revenue": total}


@router.get("/top-clients")
async def get_top_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    limit: int = 10,
):
    result = await db.execute(
        select(
            models.Client.id,
            models.Client.full_name,
            func.coalesce(func.sum(models.Service.price), 0).label("total_spent"),
            func.count(models.Appointment.id).label("visits_count"),
        )
        .select_from(models.Client)
        .join(models.Appointment, models.Appointment.client_id == models.Client.id)
        .join(models.Service, models.Appointment.service_id == models.Service.id)
        .where(
            models.Client.owner_id == current_user.id,
            models.Appointment.status == "completed",
        )
        .group_by(models.Client.id, models.Client.full_name)
        .order_by(func.sum(models.Service.price).desc())
        .limit(limit),
    )
    rows = result.all()
    return [
        {"client_id": r.id, "full_name": r.full_name, "total_spent": r.total_spent, "visits_count": r.visits_count}
        for r in rows
    ]


@router.get("/inactive-clients")
async def get_inactive_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    days: int = 30,
):
    cutoff = dt.now() - timedelta(days=days)

    result = await db.execute(
        select(
            models.Client.id,
            models.Client.full_name,
            func.max(models.Appointment.start_time).label("last_visit"),
        )
        .select_from(models.Client)
        .outerjoin(
            models.Appointment,
            (models.Appointment.client_id == models.Client.id)
            & (models.Appointment.status == "completed"),
        )
        .where(models.Client.owner_id == current_user.id)
        .group_by(models.Client.id, models.Client.full_name)
        .having((func.max(models.Appointment.start_time) < cutoff) | (func.max(models.Appointment.start_time).is_(None))),
    )
    rows = result.all()
    return [
        {"client_id": r.id, "full_name": r.full_name, "last_visit": r.last_visit}
        for r in rows
    ]