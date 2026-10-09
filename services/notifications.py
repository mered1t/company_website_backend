"""Уведомления в CRM о записях, которые делает или меняет сам клиент.

Кому: владельцам и администраторам организации (все записи салона) и мастеру этой записи, если у него есть
учётная запись (только его записи). Строки создаются в той же транзакции, что и сама запись: либо есть оба, либо нет.
Закрытой организации (доступ кончился или приостановлена) уведомления не создаются.
Когда запись создаёт или меняет сам сотрудник, уведомлений нет: он и так знает.
"""
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.time_utils import utc_now
from domain.notifications import RETENTION_DAYS, NotificationType
from domain.subscription import get_subscription


async def notify_booking(
    db: AsyncSession,
    appointment: models.Appointment,
    type_: NotificationType,
    *,
    old_start: datetime | None = None,
) -> int:
    """Добавляет уведомления получателям (без коммита, коммитит вызывающий). Возвращает, сколько получателей."""
    with db.no_autoflush:  # правки записи ещё не сохранены
        organization = await db.get(models.Organization, appointment.organization_id)
        if organization is None or not get_subscription(organization).has_access:
            return 0
        user_ids = (await db.execute(
            select(models.Membership.user_id)
            .where(
                models.Membership.organization_id == appointment.organization_id,
                or_(
                    models.Membership.role.in_([models.MembershipRole.owner, models.MembershipRole.admin]),
                    and_(
                        models.Membership.role == models.MembershipRole.master,
                        models.Membership.master_id == appointment.master_id,
                    ),
                ),
            )
            .order_by(models.Membership.id),
        )).scalars().all()
        user_ids = list(dict.fromkeys(user_ids))
        if not user_ids:
            return 0
        service = await db.get(models.Service, appointment.service_id)
        master = await db.get(models.Master, appointment.master_id)

    db.add_all([
        models.Notification(
            organization_id=appointment.organization_id,
            user_id=user_id,
            type=type_.value,
            appointment_id=appointment.id,
            client_id=appointment.client_id,
            service_name=service.name if service else "",
            master_name=master.full_name if master else "",
            start_time=appointment.start_time,
            old_start_time=old_start,
        )
        for user_id in user_ids
    ])
    return len(user_ids)


async def delete_old_notifications(db: AsyncSession) -> int:
    """Удаляет уведомления старше RETENTION_DAYS дней. Возвращает, сколько удалено."""
    result = await db.execute(
        delete(models.Notification)
        .where(models.Notification.created_at < utc_now() - timedelta(days=RETENTION_DAYS))
        .execution_options(synchronize_session=False),
    )
    await db.commit()
    return result.rowcount
