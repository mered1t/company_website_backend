"""Все данные организации: выгрузка (переносимость данных) и полное удаление (право на забвение, GDPR).

Если добавляешь новую таблицу с данными салона, её нужно добавить и сюда (в выгрузку и в удаление).
Тест test_every_table_is_classified напомнит об этом.
"""
from collections import defaultdict

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.time_utils import utc_now

EXPORT_FORMAT_VERSION = 1


def _group(rows, key) -> dict:
    grouped = defaultdict(list)
    for row in rows:
        grouped[key(row)].append(row)
    return grouped


async def build_export(db: AsyncSession, organization_id: int) -> dict:
    """Все данные салона одним словарём (даты потом превратятся в строки ISO). Пароли и токены сюда не попадают."""
    org = await db.get(models.Organization, organization_id)

    members = (await db.execute(
        select(models.Membership, models.User.username, models.User.email)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(models.Membership.organization_id == organization_id)
        .order_by(models.Membership.id),
    )).all()

    services = (await db.execute(
        select(models.Service).where(models.Service.organization_id == organization_id).order_by(models.Service.id),
    )).scalars().all()

    masters = (await db.execute(
        select(models.Master).where(models.Master.organization_id == organization_id).order_by(models.Master.id),
    )).scalars().all()
    master_ids = select(models.Master.id).where(models.Master.organization_id == organization_id)

    master_services = (await db.execute(
        select(models.MasterService).where(models.MasterService.master_id.in_(master_ids)),
    )).scalars().all()
    working_hours = (await db.execute(
        select(models.WorkingHours).where(models.WorkingHours.master_id.in_(master_ids))
        .order_by(models.WorkingHours.day_of_week, models.WorkingHours.start_time),
    )).scalars().all()
    exceptions = (await db.execute(
        select(models.WorkingHoursException).where(models.WorkingHoursException.master_id.in_(master_ids))
        .order_by(models.WorkingHoursException.date, models.WorkingHoursException.start_time),
    )).scalars().all()
    time_off = (await db.execute(
        select(models.TimeOff).where(models.TimeOff.master_id.in_(master_ids)).order_by(models.TimeOff.start_date),
    )).scalars().all()

    service_ids_by_master = _group(master_services, lambda r: r.master_id)
    hours_by_master = _group(working_hours, lambda r: r.master_id)
    exceptions_by_master = _group(exceptions, lambda r: r.master_id)
    time_off_by_master = _group(time_off, lambda r: r.master_id)

    clients = (await db.execute(
        select(models.Client).where(models.Client.organization_id == organization_id).order_by(models.Client.id),
    )).scalars().all()
    client_ids = select(models.Client.id).where(models.Client.organization_id == organization_id)
    comments = (await db.execute(
        select(models.ClientComment, models.User.username)
        .outerjoin(models.User, models.User.id == models.ClientComment.user_id)
        .where(models.ClientComment.client_id.in_(client_ids))
        .order_by(models.ClientComment.created_at, models.ClientComment.id),
    )).all()
    comments_by_client = _group(comments, lambda r: r[0].client_id)

    appointments = (await db.execute(
        select(models.Appointment).where(models.Appointment.organization_id == organization_id)
        .order_by(models.Appointment.start_time, models.Appointment.id),
    )).scalars().all()

    return {
        "format_version": EXPORT_FORMAT_VERSION,
        "exported_at": utc_now(),
        "note": "Prices are in minor units (cents). Appointment times are local time of the organization's timezone.",
        "organization": {
            "id": org.id,
            "name": org.name,
            "slug": org.slug,
            "timezone": org.timezone,
            "currency": org.currency,
            "booking_horizon_days": org.booking_horizon_days,
            "plan": org.plan,
            "created_at": org.created_at,
        },
        "members": [
            {
                "user_id": m.user_id, "username": username, "email": email, "role": m.role.value,
                "master_id": m.master_id, "joined_at": m.created_at,
            }
            for m, username, email in members
        ],
        "services": [
            {
                "id": s.id, "name": s.name, "price": s.price, "duration_minutes": s.duration_minutes,
                "description": s.description, "emoji": s.emoji, "photo": s.photo,
                "created_at": s.created_at, "deleted_at": s.deleted_at,
            }
            for s in services
        ],
        "masters": [
            {
                "id": m.id, "full_name": m.full_name, "phone": m.phone, "photo": m.photo,
                "created_at": m.created_at, "deleted_at": m.deleted_at,
                "service_ids": sorted(r.service_id for r in service_ids_by_master.get(m.id, [])),
                "working_hours": [
                    {"day_of_week": h.day_of_week, "start_time": h.start_time, "end_time": h.end_time}
                    for h in hours_by_master.get(m.id, [])
                ],
                "working_hours_exceptions": [
                    {"date": e.date, "start_time": e.start_time, "end_time": e.end_time}
                    for e in exceptions_by_master.get(m.id, [])
                ],
                "time_off": [
                    {"start_date": t.start_date, "end_date": t.end_date, "reason": t.reason}
                    for t in time_off_by_master.get(m.id, [])
                ],
            }
            for m in masters
        ],
        "clients": [
            {
                "id": c.id, "full_name": c.full_name, "phone": c.phone, "email": c.email,
                "birth_date": c.birth_date, "notes": c.notes, "created_at": c.created_at,
                "deleted_at": c.deleted_at, "anonymized_at": c.anonymized_at,
                "comments": [
                    {"content": comment.content, "author_username": username, "created_at": comment.created_at}
                    for comment, username in comments_by_client.get(c.id, [])
                ],
            }
            for c in clients
        ],
        "appointments": [
            {
                "id": a.id, "client_id": a.client_id, "service_id": a.service_id, "master_id": a.master_id,
                "start_time": a.start_time, "end_time": a.end_time, "status": a.status,
                "price": a.price, "currency": a.currency, "notes": a.notes,
                "created_at": a.created_at, "deleted_at": a.deleted_at,
            }
            for a in appointments
        ],
    }


async def delete_organization_data(db: AsyncSession, organization_id: int) -> None:
    """Безвозвратно удаляет организацию и все её данные. Коммит делает вызывающий: либо всё, либо ничего.

    Аккаунты пользователей (таблица users) остаются: человек мог состоять и в других организациях.
    Таблицы чистим явно и по порядку, не надеясь на ON DELETE CASCADE в боевой базе.
    """
    appointment_ids = select(models.Appointment.id).where(models.Appointment.organization_id == organization_id)
    client_ids = select(models.Client.id).where(models.Client.organization_id == organization_id)
    master_ids = select(models.Master.id).where(models.Master.organization_id == organization_id)

    statements = [
        delete(models.Notification).where(models.Notification.organization_id == organization_id),
        delete(models.AppointmentToken).where(models.AppointmentToken.appointment_id.in_(appointment_ids)),
        delete(models.ClientComment).where(models.ClientComment.client_id.in_(client_ids)),
        delete(models.Appointment).where(models.Appointment.organization_id == organization_id),
        delete(models.MasterService).where(models.MasterService.master_id.in_(master_ids)),
        delete(models.WorkingHours).where(models.WorkingHours.master_id.in_(master_ids)),
        delete(models.TimeOff).where(models.TimeOff.master_id.in_(master_ids)),
        delete(models.WorkingHoursException).where(models.WorkingHoursException.master_id.in_(master_ids)),
        delete(models.Membership).where(models.Membership.organization_id == organization_id),
        delete(models.Invitation).where(models.Invitation.organization_id == organization_id),
        delete(models.ActivityLog).where(models.ActivityLog.organization_id == organization_id),
        delete(models.AiReport).where(models.AiReport.organization_id == organization_id),
        delete(models.Client).where(models.Client.organization_id == organization_id),
        delete(models.Service).where(models.Service.organization_id == organization_id),
        delete(models.Master).where(models.Master.organization_id == organization_id),
        delete(models.Organization).where(models.Organization.id == organization_id),
    ]
    for statement in statements:
        await db.execute(statement.execution_options(synchronize_session=False))
