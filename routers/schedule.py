"""Расписание для CRM: свободное время без публичных ограничений (лимит запросов, горизонт записи)."""
from datetime import date as date_type, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from auth.auth import CurrentMembership
from db.database import get_db
from domain.enums import AppointmentStatus
from schemas.schemas import (CrmAvailableSlot, MenuCounters, TodayResponse, TodaySummary, TomorrowBlock,
                             UnmarkedBlock)
from services.booking import get_free_slots, load_service_and_master, masters_for_service
from services.common import get_org_now, get_owned, get_owned_active

UNMARKED_DAYS = 60   # насколько далеко назад искать неотмеченные записи
LIST_LIMIT = 50      # сколько записей отдавать в списках «завтра» и «неотмеченные»

router = APIRouter()


@router.get("/available-slots", response_model=list[CrmAvailableSlot])
async def crm_available_slots(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    service_id: int,
    date: date_type,
    master_id: int | None = None,
    exclude_appointment_id: int | None = None,
):
    """Свободное время на день для формы записи в CRM.

    Отличия от публичного available-slots: нужен вход, нет лимита 30 запросов в минуту, нет горизонта записи,
    прошедшее время дня тоже показывается (визит можно внести задним числом).
    exclude_appointment_id — при переносе: время самой этой записи считается свободным.
    Без master_id — все мастера, которые делают услугу; у каждого слота master_ids — кто свободен.
    Мастер видит только своё время (master_id подставляется сам).
    """
    org_id = membership.organization_id
    if membership.role == models.MembershipRole.master:
        if membership.master_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Your membership is not linked to a master profile")
        master_id = membership.master_id
    if exclude_appointment_id is not None:
        await get_owned(db, models.Appointment, exclude_appointment_id, org_id, "Appointment")

    if master_id is not None:
        service, _ = await load_service_and_master(db, org_id, service_id, master_id)
        master_ids = [master_id]
    else:
        service = await get_owned_active(db, models.Service, service_id, org_id, "Service")
        master_ids = [m.id for m in await masters_for_service(db, org_id, service_id)]

    free: dict[tuple[datetime, datetime], list[int]] = {}
    for mid in master_ids:
        for slot in await get_free_slots(db, mid, service.duration_minutes, date, datetime.min,
                                         exclude_id=exclude_appointment_id):
            free.setdefault(slot, []).append(mid)
    return [CrmAvailableSlot(start_time=s, end_time=e, master_ids=ids) for (s, e), ids in sorted(free.items())]


# ------------------------------------------------------------------ стартовая страница «Сегодня» и счётчики меню
def _is_master(membership: models.Membership) -> bool:
    return membership.role == models.MembershipRole.master


def _appointments(membership: models.Membership):
    """Записи салона без корзины; мастер видит только свои."""
    query = select(models.Appointment).where(
        models.Appointment.organization_id == membership.organization_id,
        models.Appointment.deleted_at.is_(None),
    )
    if _is_master(membership):
        query = query.where(models.Appointment.master_id == membership.master_id)
    return query


def _with_details(query):
    return query.options(
        selectinload(models.Appointment.client),
        selectinload(models.Appointment.service),
        selectinload(models.Appointment.master),
    )


def _unmarked(membership: models.Membership, today_start: datetime):
    """Записи прошлых дней (не старше UNMARKED_DAYS), которые так и остались scheduled."""
    return _appointments(membership).where(
        models.Appointment.status == AppointmentStatus.scheduled,
        models.Appointment.start_time < today_start,
        models.Appointment.start_time >= today_start - timedelta(days=UNMARKED_DAYS),
    )


async def _count(db: AsyncSession, query) -> int:
    return (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()


@router.get("/today", response_model=TodayResponse)
async def today_overview(db: Annotated[AsyncSession, Depends(get_db)], membership: CurrentMembership):
    """Всё для стартовой страницы CRM одним запросом (по местной дате салона). Мастер получает только свои записи."""
    from routers.analytics import birthdays_within  # здесь: роутеры не импортируют друг друга при загрузке

    now = await get_org_now(db, membership.organization_id)
    today_start = datetime.combine(now.date(), datetime.min.time())
    tomorrow_start = today_start + timedelta(days=1)
    by_time = (models.Appointment.start_time, models.Appointment.id)

    today_rows = (await db.execute(_with_details(_appointments(membership).where(
        models.Appointment.start_time >= today_start, models.Appointment.start_time < tomorrow_start,
    )).order_by(*by_time))).scalars().all()

    active = [a for a in today_rows if a.status != AppointmentStatus.cancelled]
    scheduled = [a for a in active if a.status == AppointmentStatus.scheduled]
    summary = TodaySummary(
        total=len(active),
        completed=sum(a.status == AppointmentStatus.completed for a in active),
        no_show=sum(a.status == AppointmentStatus.no_show for a in active),
        awaiting=sum(a.start_time <= now for a in scheduled),
        upcoming=sum(a.start_time > now for a in scheduled),
        cancelled=len(today_rows) - len(active),
        earned=sum(a.price for a in active if a.status == AppointmentStatus.completed),
        expected=sum(a.price for a in scheduled),
    )

    tomorrow_query = _appointments(membership).where(
        models.Appointment.start_time >= tomorrow_start,
        models.Appointment.start_time < tomorrow_start + timedelta(days=1),
        models.Appointment.status != AppointmentStatus.cancelled,
    )
    tomorrow = TomorrowBlock(
        count=await _count(db, tomorrow_query),
        items=(await db.execute(_with_details(tomorrow_query).order_by(*by_time).limit(LIST_LIMIT))).scalars().all(),
    )

    unmarked_query = _unmarked(membership, today_start)
    unmarked = UnmarkedBlock(
        total=await _count(db, unmarked_query),
        items=(await db.execute(_with_details(unmarked_query).order_by(*by_time).limit(LIST_LIMIT))).scalars().all(),
    )

    birthdays = None if _is_master(membership) else await birthdays_within(db, membership.organization_id, 0)
    return TodayResponse(date=now.date(), today=today_rows, summary=summary, tomorrow=tomorrow,
                         unmarked=unmarked, birthdays=birthdays)


@router.get("/counters", response_model=MenuCounters)
async def menu_counters(db: Annotated[AsyncSession, Depends(get_db)], membership: CurrentMembership):
    """Числа для меню одним запросом. Мастер: записи — только свои, лист ожидания — null."""
    org_id = membership.organization_id
    now = await get_org_now(db, org_id)
    today_start = datetime.combine(now.date(), datetime.min.time())

    def active(model):
        return select(model.id).where(model.organization_id == org_id, model.deleted_at.is_(None))

    waitlist = None
    if not _is_master(membership):
        waitlist = await _count(db, select(models.WaitlistEntry.id).where(
            models.WaitlistEntry.organization_id == org_id,
            models.WaitlistEntry.notified_at.is_(None),
            models.WaitlistEntry.day >= now.date(),
        ))
    return MenuCounters(
        today=await _count(db, _appointments(membership).where(
            models.Appointment.start_time >= today_start,
            models.Appointment.start_time < today_start + timedelta(days=1),
            models.Appointment.status != AppointmentStatus.cancelled,
        )),
        unmarked=await _count(db, _unmarked(membership, today_start)),
        clients=await _count(db, active(models.Client)),
        masters=await _count(db, active(models.Master)),
        services=await _count(db, active(models.Service)),
        waitlist=waitlist,
        notifications_unread=await _count(db, select(models.Notification.id).where(
            models.Notification.user_id == membership.user_id,
            models.Notification.organization_id == org_id,
            models.Notification.read_at.is_(None),
        )),
    )
