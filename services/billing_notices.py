"""Письма владельцу организации о конце пробного или оплаченного периода.

Три письма на каждый период доступа:
  * soon    — за SOON_DAYS дней до конца (пробного или оплаченного периода);
  * grace   — период закончился, идёт льготный: всё работает, но скоро закроется;
  * expired — льготный период закончился: кабинет закрыт, публичная запись выключена.

Как это работает (по тому же принципу, что и напоминания о записи):
  * в организации хранятся две отметки: billing_notice_stage (какое письмо ушло последним) и billing_notice_for
    (для какой даты конца доступа). Перед отправкой отметка «занимается» одним атомарным UPDATE, поэтому два
    параллельных запуска (два сервиса, cron + фоновая задача) не пришлют письмо дважды;
  * оплата двигает конец доступа, отметка перестаёт совпадать с ним, и в следующем периоде письма уйдут заново;
  * если сервис долго не работал, шлём только актуальное письмо, а не все пропущенные;
  * если конец доступа был больше STALE_DAYS дней назад после льготного периода, письмо уже не шлём;
  * если письмо не ушло ни одному владельцу (сбой Resend), отметка возвращается, и следующая проверка попробует снова.
Бесплатным (is_free) и приостановленным (is_blocked) организациям письма не идут.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import DateTime, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.time_utils import utc_now
from domain.subscription import GRACE_DAYS, SubscriptionStatus, get_subscription
from services.common import to_org_local
from services.email_service import _frontend_url, _send, render_email
from services.i18n import DEFAULT_LANGUAGE, email_subject

logger = logging.getLogger(__name__)

SOON_DAYS = 3      # за сколько дней до конца предупреждать
STALE_DAYS = 14    # через сколько дней после закрытия доступа письма уже не шлём
BATCH_LIMIT = 200  # за одну проверку; остальное подхватит следующая

STAGES = ("soon", "grace", "expired")  # порядок важен: письмо с большим номером «перекрывает» меньшие
_RANK = {stage: number for number, stage in enumerate(STAGES, start=1)}


def notice_stage(org, now: datetime) -> str | None:
    """Какое письмо сейчас актуально для организации (None: никакое)."""
    sub = get_subscription(org, now)
    if sub.access_until is None:
        return None
    if sub.status in (SubscriptionStatus.trial, SubscriptionStatus.active):
        return "soon" if sub.access_until - now <= timedelta(days=SOON_DAYS) else None
    if sub.status == SubscriptionStatus.grace:
        return "grace"
    if sub.status == SubscriptionStatus.expired:
        closed_at = sub.access_until + timedelta(days=GRACE_DAYS)
        return "expired" if now - closed_at <= timedelta(days=STALE_DAYS) else None
    return None  # free, blocked


def template_key(stage: str, status: SubscriptionStatus) -> str:
    if stage == "soon":
        return "billing_trial_ending" if status == SubscriptionStatus.trial else "billing_paid_ending"
    return "billing_grace" if stage == "grace" else "billing_expired"


def send_billing_notice_email(
    to_email: str,
    key: str,
    organization_name: str,
    date_text: str,
    days_left: int | None,
    language: str | None = DEFAULT_LANGUAGE,
) -> None:
    _send(
        to_email,
        email_subject(key, language, organization_name=organization_name),
        render_email(
            f"{key}.html",
            language,
            organization_name=organization_name,
            date_text=date_text,
            days_left=days_left,
            open_url=_frontend_url("/"),
        ),
    )


@dataclass
class _Candidate:
    organization_id: int
    organization_name: str
    stage: str
    key: str
    trial_ends_at: datetime
    paid_until: datetime | None
    access_until: datetime
    prev_for: datetime | None
    prev_stage: str | None
    date_text: str
    days_left: int | None
    recipients: list[tuple[str, str]]  # (email, язык)


def _date_text(value: datetime, tz_name: str) -> str:
    try:
        value = to_org_local(value, tz_name)
    except ZoneInfoNotFoundError:
        pass  # странный пояс у салона: показываем дату в UTC
    return value.strftime("%d.%m.%Y")


async def _find_candidates(db: AsyncSession, now: datetime) -> list[_Candidate]:
    org = models.Organization
    latest = func.greatest(org.trial_ends_at, func.coalesce(org.paid_until, org.trial_ends_at), type_=DateTime)
    orgs = (await db.execute(
        select(org)
        .where(
            org.is_blocked.is_(False),
            org.is_free.is_(False),
            latest <= now + timedelta(days=SOON_DAYS),
            latest + timedelta(days=GRACE_DAYS + STALE_DAYS) >= now,
        )
        .order_by(latest)
        .limit(BATCH_LIMIT),
    )).scalars().all()

    due = []
    for o in orgs:
        stage = notice_stage(o, now)
        if stage is None:
            continue
        sub = get_subscription(o, now)
        if o.billing_notice_for == sub.access_until and o.billing_notice_stage in _RANK \
                and _RANK[o.billing_notice_stage] >= _RANK[stage]:
            continue  # такое (или более позднее) письмо за этот период уже отправлено
        due.append((o, stage, sub))
    if not due:
        return []

    owners = (await db.execute(
        select(models.Membership.organization_id, models.User.email, models.User.language)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(
            models.Membership.organization_id.in_([o.id for o, _, _ in due]),
            models.Membership.role == models.MembershipRole.owner,
        )
        .order_by(models.Membership.id),
    )).all()
    recipients: dict[int, list[tuple[str, str]]] = {}
    for organization_id, email, language in owners:
        if email:
            recipients.setdefault(organization_id, []).append((email, language))

    candidates = []
    for o, stage, sub in due:
        if not recipients.get(o.id):
            logger.warning("Billing notice: organization %s has no owner email, skipping", o.id)
            continue
        shown = sub.access_until if stage == "soon" else sub.access_until + timedelta(days=GRACE_DAYS)
        candidates.append(_Candidate(
            organization_id=o.id,
            organization_name=o.name,
            stage=stage,
            key=template_key(stage, sub.status),
            trial_ends_at=o.trial_ends_at,
            paid_until=o.paid_until,
            access_until=sub.access_until,
            prev_for=o.billing_notice_for,
            prev_stage=o.billing_notice_stage,
            date_text=_date_text(shown, o.timezone),
            days_left=None if stage == "expired" else sub.days_left,
            recipients=recipients[o.id],
        ))
    return candidates


async def send_due_billing_notices(db: AsyncSession) -> int:
    """Отправляет все письма, которым пришло время. Возвращает, у скольких организаций письмо ушло."""
    candidates = await _find_candidates(db, utc_now())
    await db.rollback()  # закрываем транзакцию чтения: дальше в цикле есть commit

    org = models.Organization
    sent = 0
    for c in candidates:
        earlier_stages = [s for s in STAGES if _RANK[s] < _RANK[c.stage]]
        claim = await db.execute(
            update(org)
            .where(
                org.id == c.organization_id,
                org.trial_ends_at == c.trial_ends_at,
                org.paid_until.is_not_distinct_from(c.paid_until),  # даты не менялись с момента проверки
                org.is_blocked.is_(False),
                org.is_free.is_(False),
                or_(
                    org.billing_notice_for.is_distinct_from(c.access_until),
                    org.billing_notice_stage.is_(None),
                    org.billing_notice_stage.in_(earlier_stages),
                ),
            )
            .values(billing_notice_for=c.access_until, billing_notice_stage=c.stage)
            .execution_options(synchronize_session=False),
        )
        await db.commit()
        if claim.rowcount == 0:  # организацию изменили или письмо уже взял другой запуск
            continue

        delivered = 0
        for email, language in c.recipients:
            try:
                await asyncio.to_thread(
                    send_billing_notice_email,
                    to_email=email,
                    key=c.key,
                    organization_name=c.organization_name,
                    date_text=c.date_text,
                    days_left=c.days_left,
                    language=language,
                )
                delivered += 1
            except Exception:
                logger.exception(
                    "Failed to send billing notice (organization_id=%s, stage=%s)", c.organization_id, c.stage,
                )
        if delivered == 0:
            await db.execute(
                update(org)
                .where(
                    org.id == c.organization_id,
                    org.billing_notice_for == c.access_until,
                    org.billing_notice_stage == c.stage,
                )
                .values(billing_notice_for=c.prev_for, billing_notice_stage=c.prev_stage)
                .execution_options(synchronize_session=False),
            )
            await db.commit()
            continue
        sent += 1
    return sent
