"""Единое место для правил бронирования.

Любая запись (админка, публичная страница) проходит через эти функции,
чтобы правила не расходились.
"""
import zlib
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException, status
from sqlalchemy import Integer, cast, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from services.common import get_available_intervals, get_org_currency, get_owned_active
from domain.enums import AppointmentStatus
from domain.business import GROUP_BOOKINGS_OFF

OVERLAP_CONSTRAINT = "appointments_no_master_overlap"
MASTER_BUSY = "Master already has an appointment at this time"


async def ensure_group_bookings(db: AsyncSession, organization_id: int) -> None:
    """Несколько клиентов в одной записи — только если у салона включены групповые записи."""
    with db.no_autoflush:
        organization = await db.get(models.Organization, organization_id)
    if organization is None or not organization.group_bookings:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=GROUP_BOOKINGS_OFF)


def check_party_size(service: models.Service, party_size: int) -> None:
    """Компания не больше, чем разрешено у услуги."""
    if party_size > service.max_party_size:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"This service allows at most {service.max_party_size} "
                   f"{'person' if service.max_party_size == 1 else 'people'} per booking",
        )


def is_overlap_violation(error: IntegrityError) -> bool:
    return OVERLAP_CONSTRAINT in str(error.orig)


async def check_master_provides_service(db: AsyncSession, master_id: int, service_id: int) -> None:
    """Если у мастера задан список услуг, выбранная услуга должна быть в нём."""
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.services))
        .where(models.Master.id == master_id),
    )
    master = result.scalars().first()
    if master and master.services and service_id not in {s.id for s in master.services}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This master does not provide this service",
        )


async def check_working_hours(db: AsyncSession, master_id: int, start_time: datetime, end_time: datetime) -> None:
    intervals = await get_available_intervals(db, master_id, start_time.date())
    if not intervals:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Master does not work on this day")

    # Сравниваем полное время, а не строки "ЧЧ:ММ": иначе запись на 23:30 длиной час заканчивалась бы в "00:30",
    # что как строка «меньше» "19:00", и проходила бы проверку. Запись должна начаться и закончиться в тот же день
    # и целиком уместиться в один рабочий интервал (секунды тоже учитываются).
    if end_time <= start_time or end_time.date() != start_time.date():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Appointment time is outside master's working hours")
    start_t, end_t = start_time.time(), end_time.time()
    if not any(time.fromisoformat(s) <= start_t and end_t <= time.fromisoformat(e) for s, e in intervals):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="Appointment time is outside master's working hours")


async def check_overlap(db: AsyncSession, master_id: int, start_time: datetime, end_time: datetime,
                        exclude_id: int | None = None) -> None:
    query = select(models.Appointment).where(
        models.Appointment.master_id == master_id,
        models.Appointment.status != AppointmentStatus.cancelled,
        models.Appointment.deleted_at.is_(None),
        models.Appointment.start_time < end_time,
        models.Appointment.end_time > start_time,
    )
    if exclude_id is not None:
        query = query.where(models.Appointment.id != exclude_id)

    result = await db.execute(query)
    if result.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=MASTER_BUSY)


async def check_slot_free(db: AsyncSession, master_id: int, start_time: datetime, end_time: datetime,
                          exclude_id: int | None = None) -> None:
    """Рабочие часы + отсутствие пересечений.

    no_autoflush: проверки читают данные из базы и не должны отправлять туда
    наши ещё не сохранённые правки (иначе защита от пересечений сработает раньше,
    чем наша проверка, и вместо 400 получится 500).
    """
    with db.no_autoflush:
        await check_working_hours(db, master_id, start_time, end_time)
        await check_overlap(db, master_id, start_time, end_time, exclude_id=exclude_id)


async def persist(db: AsyncSession, *, commit: bool, detail: str = MASTER_BUSY) -> None:
    """flush или commit; нарушение защиты от двойной записи превращает в 400 вместо 500."""
    try:
        if commit:
            await db.commit()
        else:
            await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if is_overlap_violation(e):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)
        raise


async def create_appointment_record(
    db: AsyncSession,
    *,
    organization_id: int,
    client_id: int,
    service: models.Service,
    master_id: int,
    start_time: datetime,
    end_time: datetime,
    notes: str | None = None,
    booked_online: bool = False,
    participants: list[models.Client] | None = None,
) -> models.Appointment:
    """Создаёт запись со снимком цены и валюты. Единственное место, где это делается.

    participants — остальные клиенты записи (уже проверенные load_participants).
    """
    currency = await get_org_currency(db, organization_id)
    appointment = models.Appointment(
        organization_id=organization_id,
        client_id=client_id,
        service_id=service.id,
        master_id=master_id,
        start_time=start_time,
        end_time=end_time,
        price=service.price,
        currency=currency,
        notes=notes,
        booked_online=booked_online,
    )
    if participants:
        set_participants(appointment, service, participants)
    db.add(appointment)
    await persist(db, commit=False)
    return appointment


# ------------------------------------------------------------------ несколько клиентов в одной записи
async def load_participants(
    db: AsyncSession, organization_id: int, service: models.Service, main_client_id: int, client_ids: list[int],
) -> list[models.Client]:
    """Карточки остальных клиентов записи по id: свои, не удалённые, без повторов и без основного клиента.

    Непустой список можно только при включённых групповых записях и если все помещаются в услугу.
    """
    client_ids = list(dict.fromkeys(client_ids))
    if not client_ids:
        return []
    if main_client_id in client_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="The main client of the appointment is already in it")
    await ensure_group_bookings(db, organization_id)
    check_party_size(service, 1 + len(client_ids))
    with db.no_autoflush:
        return [await get_owned_active(db, models.Client, client_id, organization_id, "Client")
                for client_id in client_ids]


def set_participants(appointment: models.Appointment, service: models.Service, clients: list[models.Client]) -> None:
    """Новый список остальных клиентов записи (без коммита). Те, кто уже был в записи, остаются как есть."""
    current = {p.client_id: p for p in appointment.participants}
    appointment.participants = [
        current.get(client.id) or models.AppointmentParticipant(
            organization_id=appointment.organization_id, client_id=client.id, price=service.price, client=client,
        )
        for client in clients
    ]
    reprice(appointment, service)


def reprice(appointment: models.Appointment, service: models.Service) -> None:
    """Цена за человека: у каждого клиента своя часть, у записи — сумма за всех."""
    appointment.party_size = 1 + len(appointment.participants)
    for participant in appointment.participants:
        participant.price = service.price
    appointment.price = service.price * appointment.party_size


SLOT_STEP = timedelta(minutes=15)
OFF_GRID = "Please choose one of the offered times"


def to_salon_time(value: datetime, tz_name: str) -> datetime:
    """Время записи по часам салона (в базе оно без часового пояса).

    Если клиент прислал время с поясом (например, «...Z» из toISOString), переводим его в пояс салона,
    а не просто отбрасываем пояс: иначе запись сдвигалась бы на разницу во времени.
    """
    if value.tzinfo is None:
        return value
    return value.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)


async def check_on_slot_grid(db: AsyncSession, master_id: int, start_time: datetime) -> None:
    """Клиент записывается только на время из сетки: каждые 15 минут от начала рабочего интервала, без секунд.

    Иначе запись на 10:07 «рвёт» день мастера, и из списка свободного времени пропадают удобные слоты.
    Вызывать после check_slot_free (там уже проверено, что время внутри рабочего интервала).
    """
    if start_time.second or start_time.microsecond:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=OFF_GRID)
    for start_str, _ in await get_available_intervals(db, master_id, start_time.date()):
        interval_start = datetime.combine(start_time.date(), time.fromisoformat(start_str))
        if interval_start <= start_time and (start_time - interval_start) % SLOT_STEP == timedelta(0):
            return
    raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=OFF_GRID)


async def lock_client_phone(db: AsyncSession, organization_id: int, phone: str) -> None:
    """Публичные записи с одним телефоном в одном салоне идут строго по очереди (до конца транзакции).

    Без этого одновременные запросы обходили лимит «3 будущие записи» и падали с 500, создавая клиента дважды.
    """
    key = zlib.crc32(phone.encode()) - 2**31  # в диапазон int4
    await db.execute(select(func.pg_advisory_xact_lock(cast(organization_id, Integer), cast(key, Integer))))


async def load_service_and_master(db: AsyncSession, organization_id: int, service_id: int, master_id: int):
    """Услуга и мастер этой организации (404, если нет) + мастер оказывает эту услугу (400, если нет)."""
    service = await get_owned_active(db, models.Service, service_id, organization_id, "Service")
    master = await get_owned_active(db, models.Master, master_id, organization_id, "Master")
    await check_master_provides_service(db, master_id, service_id)
    return service, master


async def masters_for_service(db: AsyncSession, organization_id: int, service_id: int) -> list[models.Master]:
    """Активные мастера салона, которые делают услугу (пустой список услуг у мастера = делает все)."""
    service = await get_owned_active(db, models.Service, service_id, organization_id, "Service")
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.services))
        .where(models.Master.organization_id == organization_id, models.Master.deleted_at.is_(None))
        .order_by(models.Master.id),
    )
    return [m for m in result.scalars().all() if not m.services or service.id in {s.id for s in m.services}]


async def master_load(db: AsyncSession, master_id: int, day: date) -> int:
    """Сколько записей у мастера в этот день (без отменённых): по этому выбираем самого свободного."""
    return len(await get_busy_appointments(db, master_id, day))


async def get_busy_appointments(
    db: AsyncSession, master_id: int, day: date, exclude_id: int | None = None,
) -> list[models.Appointment]:
    day_start = datetime.combine(day, datetime.min.time())
    day_end = datetime.combine(day, datetime.max.time().replace(microsecond=0))
    query = select(models.Appointment).where(
        models.Appointment.master_id == master_id,
        models.Appointment.status != AppointmentStatus.cancelled,
        models.Appointment.deleted_at.is_(None),
        models.Appointment.start_time < day_end,
        models.Appointment.end_time > day_start,
    )
    if exclude_id is not None:
        query = query.where(models.Appointment.id != exclude_id)  # при переносе сама запись не должна занимать слот
    result = await db.execute(query)
    return result.scalars().all()


async def get_free_slots(
    db: AsyncSession,
    master_id: int,
    duration_minutes: int,
    day: date,
    now: datetime,
    *,
    first_only: bool = False,
    exclude_id: int | None = None,
) -> list[tuple[datetime, datetime]]:
    """Свободные слоты мастера на день. first_only=True останавливается на первом найденном."""
    intervals = await get_available_intervals(db, master_id, day)
    if not intervals:
        return []

    busy = await get_busy_appointments(db, master_id, day, exclude_id=exclude_id)
    duration = timedelta(minutes=duration_minutes)

    slots: list[tuple[datetime, datetime]] = []
    for start_str, end_str in intervals:
        interval_start = datetime.combine(day, datetime.strptime(start_str, "%H:%M").time())
        interval_end = datetime.combine(day, datetime.strptime(end_str, "%H:%M").time())

        current = interval_start
        while current + duration <= interval_end:
            slot_end = current + duration
            is_free = not any(current < a.end_time and slot_end > a.start_time for a in busy)
            if is_free and current > now:
                slots.append((current, slot_end))
                if first_only:
                    return slots
            current += SLOT_STEP
    return slots