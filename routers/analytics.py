from datetime import date as date_type, datetime as dt, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentMembership, require_role
from auth.auth import ManagerMembership
from common import get_org_now, get_org_currency
from db.database import get_db
from enums import AppointmentStatus

from schemas.schemas import (
    RevenueResponse,
    TopClientResponse,
    InactiveClientResponse,
    PopularServiceResponse,
    MasterWorkloadResponse,
    UpcomingBirthdayResponse
)

router = APIRouter()


@router.get("/revenue", response_model=RevenueResponse)
async def get_revenue(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
    date_from: dt,
    date_to: dt,
):
    currency = await get_org_currency(db, membership.organization_id)

    result = await db.execute(
        select(func.coalesce(func.sum(models.Appointment.price), 0))
        .where(
            models.Appointment.organization_id == membership.organization_id,
            models.Appointment.status == AppointmentStatus.completed,
            models.Appointment.start_time >= date_from,
            models.Appointment.start_time <= date_to,
            models.Appointment.currency == currency,
        ),
    )
    total = result.scalar()
    return {"date_from": date_from, "date_to": date_to, "total_revenue": total}


@router.get("/top-clients", response_model=list[TopClientResponse])
async def get_top_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
    limit: int = 10,
):
    currency = await get_org_currency(db, membership.organization_id)

    result = await db.execute(
        select(
            models.Client.id,
            models.Client.full_name,
            func.coalesce(func.sum(models.Appointment.price), 0).label("total_spent"),
            func.count(models.Appointment.id).label("visits_count"),
        )
        .select_from(models.Client)
        .join(models.Appointment, models.Appointment.client_id == models.Client.id)
        .where(
            models.Client.organization_id == membership.organization_id,
            models.Appointment.status == AppointmentStatus.completed,
            models.Appointment.currency == currency,
        )
        .group_by(models.Client.id, models.Client.full_name)
        .order_by(func.sum(models.Appointment.price).desc())
        .limit(limit),
    )
    rows = result.all()
    return [
        {"client_id": r.id, "full_name": r.full_name, "total_spent": r.total_spent, "visits_count": r.visits_count}
        for r in rows
    ]


@router.get("/inactive-clients", response_model=list[InactiveClientResponse])
async def get_inactive_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
    days: int = 30,
):
    org_now = await get_org_now(db, membership.organization_id)
    cutoff = org_now - timedelta(days=days)

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
            & (models.Appointment.status == AppointmentStatus.completed),
        )
        .where(models.Client.organization_id == membership.organization_id)
        .group_by(models.Client.id, models.Client.full_name)
        .having((func.max(models.Appointment.start_time) < cutoff) | (func.max(models.Appointment.start_time).is_(None))),
    )
    rows = result.all()
    return [
        {"client_id": r.id, "full_name": r.full_name, "last_visit": r.last_visit}
        for r in rows
    ]


@router.get("/popular-services", response_model=list[PopularServiceResponse])
async def get_popular_services(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
    limit: int = 10,
):
    currency = await get_org_currency(db, membership.organization_id)

    result = await db.execute(
        select(
            models.Service.id,
            models.Service.name,
            func.count(models.Appointment.id).label("times_booked"),
            func.coalesce(func.sum(models.Appointment.price), 0).label("total_revenue"),
        )
        .select_from(models.Service)
        .join(models.Appointment, models.Appointment.service_id == models.Service.id)
        .where(
            models.Service.organization_id == membership.organization_id,
            models.Appointment.status == AppointmentStatus.completed,
            models.Appointment.currency == currency,
        )
        .group_by(models.Service.id, models.Service.name)
        .order_by(func.count(models.Appointment.id).desc())
        .limit(limit),
    )
    rows = result.all()
    return [
        {"service_id": r.id, "name": r.name, "times_booked": r.times_booked, "total_revenue": r.total_revenue}
        for r in rows
    ]


@router.get("/masters-workload", response_model=list[MasterWorkloadResponse])
async def get_masters_workload(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
    date_from: dt,
    date_to: dt,
):
    currency = await get_org_currency(db, membership.organization_id)

    result = await db.execute(
        select(
            models.Master.id,
            models.Master.full_name,
            func.count(models.Appointment.id).label("appointments_count"),
            func.coalesce(func.sum(models.Appointment.price), 0).label("total_revenue"),
        )
        .select_from(models.Master)
        .outerjoin(
            models.Appointment,
            (models.Appointment.master_id == models.Master.id)
            & (models.Appointment.status == AppointmentStatus.completed)
            & (models.Appointment.currency == currency)
            & (models.Appointment.start_time >= date_from)
            & (models.Appointment.start_time <= date_to),
        )
        .where(models.Master.organization_id == membership.organization_id)
        .group_by(models.Master.id, models.Master.full_name)
        .order_by(func.sum(models.Appointment.price).desc().nulls_last()),
    )
    rows = result.all()
    return [
        {"master_id": r.id, "full_name": r.full_name, "appointments_count": r.appointments_count, "total_revenue": r.total_revenue}
        for r in rows
    ]


def _next_birthday(birth: date_type, today: date_type) -> date_type:
    def in_year(year: int) -> date_type:
        try:
            return birth.replace(year=year)
        except ValueError:  # 29 февраля в невисокосный год
            return date_type(year, 2, 28)

    candidate = in_year(today.year)
    if candidate < today:
        candidate = in_year(today.year + 1)
    return candidate


@router.get("/birthdays", response_model=list[UpcomingBirthdayResponse])
async def get_birthdays(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
    days: int = Query(default=0, ge=0, le=60),
):
    today = (await get_org_now(db, membership.organization_id)).date()

    result = await db.execute(
        select(models.Client).where(
            models.Client.organization_id == membership.organization_id,
            models.Client.deleted_at.is_(None),
            models.Client.birth_date.is_not(None),
        ),
    )

    birthdays = []
    for client in result.scalars().all():
        birth = client.birth_date.date() if isinstance(client.birth_date, dt) else client.birth_date
        next_bd = _next_birthday(birth, today)
        days_until = (next_bd - today).days
        if days_until <= days:
            birthdays.append(
                UpcomingBirthdayResponse(
                    client_id=client.id,
                    full_name=client.full_name,
                    phone=client.phone,
                    birth_date=birth,
                    days_until=days_until,
                    turning_age=next_bd.year - birth.year,
                ),
            )

    birthdays.sort(key=lambda b: (b.days_until, b.full_name))
    return birthdays