"""Аналитика салона: read-only функции без привязки к HTTP.

Их вызывают REST-эндпоинты (routers/analytics.py) и в будущем ИИ-слой.
org_id всегда берётся из авторизации на сервере, а не из параметров пользователя.
start_time хранится как локальное время салона, поэтому группируем без пересчёта таймзон.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, extract, func, literal_column, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from common import get_org_currency, get_org_now
from enums import AppointmentStatus

GROUP_BY_VALUES = ("day", "week", "month")

A = models.Appointment


@dataclass(frozen=True)
class Period:
    date_from: datetime
    date_to: datetime


def make_period(date_from: datetime, date_to: datetime) -> Period:
    # в БД время без таймзоны (локальное время салона), поэтому tzinfo отбрасываем
    date_from = date_from.replace(tzinfo=None)
    date_to = date_to.replace(tzinfo=None)
    if date_from > date_to:
        raise ValueError("date_from must not be later than date_to")
    return Period(date_from, date_to)


def _period_conditions(period: Period | None) -> list:
    if period is None:
        return []
    return [A.start_time >= period.date_from, A.start_time <= period.date_to]


async def _revenue_sum(db: AsyncSession, org_id: int, currency: str, period: Period) -> int:
    result = await db.execute(
        select(func.coalesce(func.sum(A.price), 0)).where(
            A.organization_id == org_id,
            A.status == AppointmentStatus.completed,
            A.deleted_at.is_(None),
            A.currency == currency,
            *_period_conditions(period),
        ),
    )
    return int(result.scalar())


async def revenue_total(db: AsyncSession, org_id: int, period: Period) -> dict:
    currency = await get_org_currency(db, org_id)
    total = await _revenue_sum(db, org_id, currency, period)
    return {
        "date_from": period.date_from,
        "date_to": period.date_to,
        "total_revenue": total,
        "currency": currency,
    }


def _bucket_start(value: datetime, group_by: str) -> datetime:
    day = value.replace(hour=0, minute=0, second=0, microsecond=0)
    if group_by == "week":
        return day - timedelta(days=day.weekday())
    if group_by == "month":
        return day.replace(day=1)
    return day


def _next_bucket(value: datetime, group_by: str) -> datetime:
    if group_by == "week":
        return value + timedelta(days=7)
    if group_by == "month":
        return (value.replace(day=1) + timedelta(days=32)).replace(day=1)
    return value + timedelta(days=1)


async def revenue_trend(db: AsyncSession, org_id: int, period: Period, group_by: str) -> dict:
    assert group_by in GROUP_BY_VALUES  # значение вставляется в SQL как литерал, поэтому только из белого списка
    currency = await get_org_currency(db, org_id)

    bucket = func.date_trunc(literal_column(f"'{group_by}'"), A.start_time)
    rows = (await db.execute(
        select(
            bucket.label("bucket"),
            func.coalesce(func.sum(A.price), 0).label("revenue"),
            func.count(A.id).label("cnt"),
        )
        .where(
            A.organization_id == org_id,
            A.status == AppointmentStatus.completed,
            A.deleted_at.is_(None),
            A.currency == currency,
            *_period_conditions(period),
        )
        .group_by(bucket)
        .order_by(bucket),
    )).all()
    by_bucket = {r.bucket: (int(r.revenue), int(r.cnt)) for r in rows}

    points = []
    current = _bucket_start(period.date_from, group_by)
    while current <= period.date_to:
        revenue, count = by_bucket.get(current, (0, 0))
        points.append({"period_start": current, "revenue": revenue, "appointments": count})
        current = _next_bucket(current, group_by)

    total = sum(p["revenue"] for p in points)

    # предыдущий период такой же длины, чтобы сравнивать «яблоки с яблоками»
    length = period.date_to - period.date_from
    prev_to = period.date_from - timedelta(microseconds=1)
    prev_from = prev_to - length
    previous_total = await _revenue_sum(db, org_id, currency, Period(prev_from, prev_to))
    change_percent = round((total - previous_total) / previous_total * 100, 1) if previous_total > 0 else None

    return {
        "date_from": period.date_from,
        "date_to": period.date_to,
        "group_by": group_by,
        "currency": currency,
        "total_revenue": total,
        "previous_total_revenue": previous_total,
        "change_percent": change_percent,
        "points": points,
    }


async def top_clients(db: AsyncSession, org_id: int, limit: int, period: Period | None = None) -> list[dict]:
    currency = await get_org_currency(db, org_id)
    result = await db.execute(
        select(
            models.Client.id,
            models.Client.full_name,
            func.coalesce(func.sum(A.price), 0).label("total_spent"),
            func.count(A.id).label("visits_count"),
        )
        .select_from(models.Client)
        .join(A, A.client_id == models.Client.id)
        .where(
            models.Client.organization_id == org_id,
            models.Client.deleted_at.is_(None),
            A.status == AppointmentStatus.completed,
            A.deleted_at.is_(None),
            A.currency == currency,
            *_period_conditions(period),
        )
        .group_by(models.Client.id, models.Client.full_name)
        .order_by(func.sum(A.price).desc(), models.Client.id)
        .limit(limit),
    )
    return [
        {"client_id": r.id, "full_name": r.full_name, "total_spent": r.total_spent, "visits_count": r.visits_count}
        for r in result.all()
    ]


async def inactive_clients(db: AsyncSession, org_id: int, days: int, limit: int) -> list[dict]:
    org_now = await get_org_now(db, org_id)
    cutoff = org_now - timedelta(days=days)
    last_visit = func.max(A.start_time)

    result = await db.execute(
        select(models.Client.id, models.Client.full_name, last_visit.label("last_visit"))
        .select_from(models.Client)
        .outerjoin(
            A,
            (A.client_id == models.Client.id)
            & (A.deleted_at.is_(None))
            & (A.status == AppointmentStatus.completed),
        )
        .where(
            models.Client.organization_id == org_id,
            models.Client.deleted_at.is_(None),
        )
        .group_by(models.Client.id, models.Client.full_name)
        .having((last_visit < cutoff) | (last_visit.is_(None)))
        # сначала те, кто был недавно: их вернуть проще всего; клиенты без визитов в конце
        .order_by(last_visit.desc().nulls_last(), models.Client.id)
        .limit(limit),
    )
    return [{"client_id": r.id, "full_name": r.full_name, "last_visit": r.last_visit} for r in result.all()]


async def popular_services(db: AsyncSession, org_id: int, limit: int, period: Period | None = None) -> list[dict]:
    currency = await get_org_currency(db, org_id)
    result = await db.execute(
        select(
            models.Service.id,
            models.Service.name,
            func.count(A.id).label("times_booked"),
            func.coalesce(func.sum(A.price), 0).label("total_revenue"),
        )
        .select_from(models.Service)
        .join(A, A.service_id == models.Service.id)
        .where(
            models.Service.organization_id == org_id,
            A.status == AppointmentStatus.completed,
            A.deleted_at.is_(None),
            A.currency == currency,
            *_period_conditions(period),
        )
        .group_by(models.Service.id, models.Service.name)
        .order_by(func.count(A.id).desc(), models.Service.id)
        .limit(limit),
    )
    return [
        {"service_id": r.id, "name": r.name, "times_booked": r.times_booked, "total_revenue": r.total_revenue}
        for r in result.all()
    ]


async def masters_workload(db: AsyncSession, org_id: int, period: Period) -> list[dict]:
    currency = await get_org_currency(db, org_id)
    result = await db.execute(
        select(
            models.Master.id,
            models.Master.full_name,
            func.count(A.id).label("appointments_count"),
            func.coalesce(func.sum(A.price), 0).label("total_revenue"),
        )
        .select_from(models.Master)
        .outerjoin(
            A,
            (A.master_id == models.Master.id)
            & (A.deleted_at.is_(None))
            & (A.status == AppointmentStatus.completed)
            & (A.currency == currency)
            & (A.start_time >= period.date_from)
            & (A.start_time <= period.date_to),
        )
        .where(models.Master.organization_id == org_id)
        .group_by(models.Master.id, models.Master.full_name)
        .order_by(func.sum(A.price).desc().nulls_last()),
    )
    return [
        {
            "master_id": r.id,
            "full_name": r.full_name,
            "appointments_count": r.appointments_count,
            "total_revenue": r.total_revenue,
        }
        for r in result.all()
    ]


async def appointments_summary(db: AsyncSession, org_id: int, period: Period) -> dict:
    currency = await get_org_currency(db, org_id)
    paid = and_(A.status == AppointmentStatus.completed, A.currency == currency)

    row = (await db.execute(
        select(
            func.count(A.id).label("total"),
            func.count(A.id).filter(A.status == AppointmentStatus.completed).label("completed"),
            func.count(A.id).filter(A.status == AppointmentStatus.cancelled).label("cancelled"),
            func.count(A.id).filter(A.status == AppointmentStatus.scheduled).label("scheduled"),
            func.count(A.id).filter(paid).label("paid_count"),
            func.coalesce(func.sum(A.price).filter(paid), 0).label("revenue"),
        ).where(
            A.organization_id == org_id,
            A.deleted_at.is_(None),
            *_period_conditions(period),
        ),
    )).one()

    total = int(row.total)
    paid_count = int(row.paid_count)
    return {
        "date_from": period.date_from,
        "date_to": period.date_to,
        "currency": currency,
        "total": total,
        "completed": int(row.completed),
        "cancelled": int(row.cancelled),
        "scheduled": int(row.scheduled),
        "cancellation_rate_percent": round(int(row.cancelled) / total * 100, 1) if total else 0.0,
        "average_check": round(int(row.revenue) / paid_count) if paid_count else 0,
    }


async def clients_summary(db: AsyncSession, org_id: int, period: Period) -> dict:
    """Новые = первый завершённый визит попал в период; вернувшиеся = были и раньше."""
    base = [A.organization_id == org_id, A.deleted_at.is_(None), A.status == AppointmentStatus.completed]

    first_visit = (
        select(A.client_id, func.min(A.start_time).label("first_visit"))
        .where(*base)
        .group_by(A.client_id)
        .subquery()
    )
    active_in_period = select(A.client_id).where(*base, *_period_conditions(period))

    row = (await db.execute(
        select(
            func.count().filter(first_visit.c.first_visit >= period.date_from).label("new_clients"),
            func.count().filter(first_visit.c.first_visit < period.date_from).label("returning_clients"),
        )
        .select_from(first_visit)
        .where(first_visit.c.client_id.in_(active_in_period)),
    )).one()

    new_clients = int(row.new_clients)
    returning = int(row.returning_clients)
    total = new_clients + returning
    return {
        "date_from": period.date_from,
        "date_to": period.date_to,
        "new_clients": new_clients,
        "returning_clients": returning,
        "total_clients": total,
        "returning_share_percent": round(returning / total * 100, 1) if total else 0.0,
    }


async def busiest_hours(db: AsyncSession, org_id: int, period: Period) -> list[dict]:
    """Записи (кроме отменённых) по дню недели (0 = понедельник) и часу начала."""
    dow = extract("isodow", A.start_time)
    hour = extract("hour", A.start_time)
    result = await db.execute(
        select(dow.label("dow"), hour.label("hour"), func.count(A.id).label("cnt"))
        .where(
            A.organization_id == org_id,
            A.deleted_at.is_(None),
            A.status != AppointmentStatus.cancelled,
            *_period_conditions(period),
        )
        .group_by(dow, hour)
        .order_by(dow, hour),
    )
    return [{"weekday": int(r.dow) - 1, "hour": int(r.hour), "appointments": int(r.cnt)} for r in result.all()]
