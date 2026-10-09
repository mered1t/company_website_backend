"""Подписка: запись платежей, доступ публичной записи, SQL-условие «у организации есть доступ»."""
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import DateTime, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.time_utils import utc_now
from domain.subscription import GRACE_DAYS, add_months, get_subscription
from schemas.billing import PaymentCreate


def ensure_booking_enabled(org: "models.Organization") -> None:
    """Публичная запись работает, пока у салона есть доступ (триал, оплата или льготный период)."""
    if not get_subscription(org).booking_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Online booking is currently unavailable for this business",
        )


def access_filter(now: datetime | None = None):
    """SQL-условие «у организации есть доступ». Должно совпадать с get_subscription (это проверяет тест)."""
    now = now or utc_now()
    org = models.Organization
    latest = func.greatest(org.trial_ends_at, func.coalesce(org.paid_until, org.trial_ends_at), type_=DateTime)
    return and_(
        org.is_blocked.is_(False),
        or_(org.is_free.is_(True), latest + timedelta(days=GRACE_DAYS) >= now),
    )


async def record_payment(
    db: AsyncSession, organization_id: int, admin_id: int, data: PaymentCreate,
) -> tuple["models.Organization", "models.Payment"]:
    """Записывает платёж и двигает paid_until. Коммит делает вызывающий.

    Новый период начинается с самой поздней из дат: сейчас, конец оплаченного периода, конец пробного периода.
    Поэтому досрочная оплата не сгорает, а оплата в льготный период начинается с сегодняшнего дня.
    """
    org = (await db.execute(
        select(models.Organization).where(models.Organization.id == organization_id).with_for_update(),
    )).scalars().first()
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    now = utc_now()
    start = max(d for d in (now, org.paid_until, org.trial_ends_at) if d is not None)
    end = add_months(start, data.months)

    payment = models.Payment(
        organization_id=org.id,
        organization_name=org.name,
        amount=data.amount,
        currency=data.currency,
        method=data.method,
        plan=data.plan.value,
        months=data.months,
        paid_at=datetime(data.paid_at.year, data.paid_at.month, data.paid_at.day) if data.paid_at else now,
        period_start=start,
        period_end=end,
        note=data.note,
        recorded_by=admin_id,
    )
    db.add(payment)
    org.paid_until = end
    org.plan = data.plan.value
    await db.flush()
    return org, payment
