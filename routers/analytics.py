from datetime import date as date_type, datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import ai_service
import analytics_service as svc
import models
from auth.auth import ManagerMembership
from common import get_org_now
from db.database import get_db

from schemas.schemas import (
    RevenueResponse,
    RevenueTrendResponse,
    AppointmentsSummaryResponse,
    ClientsSummaryResponse,
    BusiestHourCell,
    TopClientResponse,
    InactiveClientResponse,
    PopularServiceResponse,
    MasterWorkloadResponse,
    UpcomingBirthdayResponse,
    AiReportRequest,
    AiReportResponse,
    AiUsageResponse,
)

router = APIRouter()

MAX_TREND_DAYS = 366


def _period(date_from: dt, date_to: dt) -> svc.Period:
    try:
        return svc.make_period(date_from, date_to)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _optional_period(date_from: dt | None = None, date_to: dt | None = None) -> svc.Period | None:
    if date_from is None and date_to is None:
        return None
    if date_from is None or date_to is None:
        raise HTTPException(status_code=422, detail="Pass both date_from and date_to, or neither")
    return _period(date_from, date_to)


PeriodDep = Annotated[svc.Period, Depends(_period)]
OptionalPeriodDep = Annotated[svc.Period | None, Depends(_optional_period)]
DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/revenue", response_model=RevenueResponse)
async def get_revenue(db: DB, membership: ManagerMembership, period: PeriodDep):
    return await svc.revenue_total(db, membership.organization_id, period)


@router.get("/revenue-trend", response_model=RevenueTrendResponse)
async def get_revenue_trend(
    db: DB,
    membership: ManagerMembership,
    period: PeriodDep,
    group_by: Literal["day", "week", "month"] = "day",
):
    if (period.date_to - period.date_from).days > MAX_TREND_DAYS:
        raise HTTPException(status_code=422, detail=f"Period must not exceed {MAX_TREND_DAYS} days")
    return await svc.revenue_trend(db, membership.organization_id, period, group_by)


@router.get("/appointments-summary", response_model=AppointmentsSummaryResponse)
async def get_appointments_summary(db: DB, membership: ManagerMembership, period: PeriodDep):
    return await svc.appointments_summary(db, membership.organization_id, period)


@router.get("/clients-summary", response_model=ClientsSummaryResponse)
async def get_clients_summary(db: DB, membership: ManagerMembership, period: PeriodDep):
    return await svc.clients_summary(db, membership.organization_id, period)


@router.get("/busiest-hours", response_model=list[BusiestHourCell])
async def get_busiest_hours(db: DB, membership: ManagerMembership, period: PeriodDep):
    return await svc.busiest_hours(db, membership.organization_id, period)


@router.get("/top-clients", response_model=list[TopClientResponse])
async def get_top_clients(
    db: DB,
    membership: ManagerMembership,
    period: OptionalPeriodDep,
    limit: int = Query(default=10, ge=1, le=100),
):
    return await svc.top_clients(db, membership.organization_id, limit, period)


@router.get("/inactive-clients", response_model=list[InactiveClientResponse])
async def get_inactive_clients(
    db: DB,
    membership: ManagerMembership,
    days: int = Query(default=30, ge=1, le=3650),
    limit: int = Query(default=100, ge=1, le=500),
):
    return await svc.inactive_clients(db, membership.organization_id, days, limit)


@router.get("/popular-services", response_model=list[PopularServiceResponse])
async def get_popular_services(
    db: DB,
    membership: ManagerMembership,
    period: OptionalPeriodDep,
    limit: int = Query(default=10, ge=1, le=100),
):
    return await svc.popular_services(db, membership.organization_id, limit, period)


@router.get("/masters-workload", response_model=list[MasterWorkloadResponse])
async def get_masters_workload(db: DB, membership: ManagerMembership, period: PeriodDep):
    return await svc.masters_workload(db, membership.organization_id, period)


@router.get("/ai-usage", response_model=AiUsageResponse)
async def get_ai_usage(db: DB, membership: ManagerMembership):
    return await ai_service.get_usage(db, membership.organization_id)


@router.post("/ai-report", response_model=AiReportResponse)
async def create_ai_report(db: DB, membership: ManagerMembership, payload: AiReportRequest):
    return await ai_service.generate_report(db, membership, payload.date_from, payload.date_to, payload.language)


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