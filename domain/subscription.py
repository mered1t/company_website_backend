"""Подписка организации. Статус не хранится, а считается по датам на лету (фоновых задач не нужно).

Как это работает:
  * при создании организации даётся пробный период (TRIAL_DAYS дней, без карты), на нём доступны все функции, включая ИИ;
  * оплата (наличные, перевод, позже карта) двигает paid_until вперёд; данные никогда не удаляются;
  * после конца оплаченного или пробного периода идёт льготный период GRACE_DAYS дней: всё работает, фронтенд
    показывает баннер «оплатите»;
  * потом статус expired: API отвечает 402, публичная запись выключена, данные целы. Оплата возвращает доступ.
Админ платформы может выдать бесплатный доступ (is_free) или приостановить организацию (is_blocked).
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from core.time_utils import utc_now
from domain.plans import Plan, plan_includes_ai

TRIAL_DAYS = 14
GRACE_DAYS = 7
TRIAL_PLAN = Plan.pro  # на пробном периоде доступно всё, включая ИИ


class SubscriptionStatus(StrEnum):
    trial = "trial"      # пробный период
    active = "active"    # оплачено
    grace = "grace"      # срок вышел, но ещё идёт льготный период: всё работает
    expired = "expired"  # доступ закрыт (402), публичная запись выключена
    free = "free"        # бесплатный доступ от админа платформы
    blocked = "blocked"  # приостановлено админом платформы


ACCESS_STATUSES = frozenset({
    SubscriptionStatus.trial, SubscriptionStatus.active, SubscriptionStatus.grace, SubscriptionStatus.free,
})


def default_trial_end() -> datetime:
    """Конец пробного периода для новой организации."""
    return utc_now() + timedelta(days=TRIAL_DAYS)


def add_months(value: datetime, months: int) -> datetime:
    """Прибавляет календарные месяцы; 31 января + 1 месяц = 28 (29) февраля."""
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


@dataclass(frozen=True)
class Subscription:
    status: SubscriptionStatus
    plan: str                      # тариф организации (за что платит)
    effective_plan: str            # тариф, по которому работают функции прямо сейчас (на триале это pro)
    ai_enabled: bool
    has_access: bool               # можно ли работать в кабинете
    booking_enabled: bool          # работает ли публичная запись
    trial_ends_at: datetime | None
    paid_until: datetime | None
    access_until: datetime | None  # когда закончится полный доступ (потом льготный период)
    grace_ends_at: datetime | None  # когда доступ закроется
    days_left: int | None          # целых дней до ближайшей границы (для баннера); None, если границы нет


def _days(delta: timedelta) -> int:
    return max(0, math.ceil(delta.total_seconds() / 86400))


def access_until_of(trial_ends_at: datetime | None, paid_until: datetime | None) -> datetime | None:
    dates = [d for d in (trial_ends_at, paid_until) if d is not None]
    return max(dates) if dates else None


def get_subscription(org, now: datetime | None = None) -> Subscription:
    """org: что угодно с полями plan, trial_ends_at, paid_until, is_free, is_blocked. now: UTC без часового пояса."""
    now = now or utc_now()
    access_until = access_until_of(org.trial_ends_at, org.paid_until)
    grace_end = access_until + timedelta(days=GRACE_DAYS) if access_until else None

    if org.is_blocked:
        status = SubscriptionStatus.blocked
    elif org.is_free:
        status = SubscriptionStatus.free
    elif org.paid_until is not None and org.paid_until > now:
        status = SubscriptionStatus.active
    elif org.trial_ends_at is not None and org.trial_ends_at > now:
        status = SubscriptionStatus.trial
    elif grace_end is not None and now <= grace_end:
        status = SubscriptionStatus.grace
    else:
        status = SubscriptionStatus.expired

    effective_plan = TRIAL_PLAN.value if status == SubscriptionStatus.trial else org.plan
    has_access = status in ACCESS_STATUSES

    if status in (SubscriptionStatus.trial, SubscriptionStatus.active):
        days_left = _days(access_until - now)
    elif status == SubscriptionStatus.grace:
        days_left = _days(grace_end - now)
    elif status == SubscriptionStatus.expired:
        days_left = 0
    else:
        days_left = None

    return Subscription(
        status=status,
        plan=org.plan,
        effective_plan=effective_plan,
        ai_enabled=has_access and plan_includes_ai(effective_plan),
        has_access=has_access,
        booking_enabled=has_access,
        trial_ends_at=org.trial_ends_at,
        paid_until=org.paid_until,
        access_until=access_until,
        grace_ends_at=grace_end if status in (
            SubscriptionStatus.trial, SubscriptionStatus.active, SubscriptionStatus.grace,
        ) else None,
        days_left=days_left,
    )
