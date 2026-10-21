from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from core.config import settings
from core.time_utils import utc_now
from db.database import get_db
from schemas.schemas import (ServicePublic,
                             MasterPublicInfo,
                             AvailableSlot,
                             PublicBookingRequest,
                             AppointmentPublic,
                             PublicOrganizationInfo, OpeningHours)


from datetime import date as date_type, timedelta
from services.common import log_activity, check_booking_horizon, get_org_now, get_owned_active
from domain.enums import AppointmentStatus

from services.booking import (
    load_service_and_master, get_free_slots, master_load, masters_for_service,
)

from services.booking import (
    check_on_slot_grid, check_party_size, check_slot_free, create_appointment_record, lock_client_phone, to_salon_time,
)

from core.rate_limiter import limiter
from services.email_service import safe_send, send_booking_confirmation_email
from services.email_view import booking_links, booking_view
from services.booking_tokens import create_token
from services.i18n import Language
from services.translations import localize
from services.billing import ensure_booking_enabled
from services.notifications import notify_booking
from domain.notifications import NotificationType
from domain.subscription import get_subscription


router = APIRouter()

NO_FREE_MASTER = "No master is available at this time. Please choose another time"
PHONE_DAILY_LIMIT = 5        # онлайн-записей с одного номера в салон за сутки
MAX_DATES_RANGE_DAYS = 31    # самый длинный период для available-dates с from/to


async def get_organization_by_slug(slug: str, db: AsyncSession, *, require_booking: bool = True) -> models.Organization:
    result = await db.execute(select(models.Organization).where(models.Organization.slug == slug))
    org = result.scalars().first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    if require_booking:
        ensure_booking_enabled(org)  # подписка закончилась: публичная запись выключена
    return org


@router.get("/{slug}/services", response_model=list[ServicePublic])
@limiter.limit("30/minute")
async def public_list_services(
    request: Request, slug: str, db: Annotated[AsyncSession, Depends(get_db)], lang: Language | None = None,
):
    """lang — язык страницы: name и description на этом языке, если у услуги есть перевод, иначе исходные."""
    org = await get_organization_by_slug(slug, db)
    result = await db.execute(
        select(models.Service).where(
            models.Service.organization_id == org.id,
            models.Service.deleted_at.is_(None),
        ),
    )
    return [localize(ServicePublic.model_validate(s), s.translations, lang) for s in result.scalars().all()]


@router.get("/{slug}/masters", response_model=list[MasterPublicInfo])
@limiter.limit("30/minute")
async def public_list_masters(
    request: Request, slug: str, db: Annotated[AsyncSession, Depends(get_db)], lang: Language | None = None,
):
    org = await get_organization_by_slug(slug, db)
    result = await db.execute(
        select(models.Master)
        .options(selectinload(models.Master.working_hours), selectinload(models.Master.services))
        .where(
            models.Master.organization_id == org.id,
            models.Master.deleted_at.is_(None),
        ),
    )
    masters = result.scalars().all()
    if not lang:
        return masters
    out = []
    for master in masters:
        info = MasterPublicInfo.model_validate(master)
        by_id = {s.id: s for s in master.services}
        info.services = [localize(item, by_id[item.id].translations, lang) for item in info.services]
        out.append(info)
    return out


@router.get("/{slug}/available-slots", response_model=list[AvailableSlot])
@limiter.limit("30/minute")
async def get_available_slots(
    request: Request,
    slug: str,
    service_id: int,
    date: date_type,
    db: Annotated[AsyncSession, Depends(get_db)],
    master_id: int | None = None,
):
    """Свободное время на день. Без master_id — «любой мастер»: общее время всех мастеров, которые делают услугу."""
    org = await get_organization_by_slug(slug, db)
    org_now = await get_org_now(db, org.id)
    await check_booking_horizon(db, org.id, date)

    if master_id is not None:
        service, _ = await load_service_and_master(db, org.id, service_id, master_id)
        slots = await get_free_slots(db, master_id, service.duration_minutes, date, org_now)
    else:
        service = await get_owned_active(db, models.Service, service_id, org.id, "Service")
        merged: set[tuple] = set()
        for master in await masters_for_service(db, org.id, service_id):
            merged.update(await get_free_slots(db, master.id, service.duration_minutes, date, org_now))
        slots = sorted(merged)
    return [AvailableSlot(start_time=s, end_time=e) for s, e in slots]


@router.post("/{slug}/book", response_model=AppointmentPublic, status_code=status.HTTP_201_CREATED)
@limiter.limit("3/hour")
async def public_create_booking(
    request: Request,
    slug: str,
    booking: PublicBookingRequest,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if booking.website:  # скрытое поле-ловушка заполняют только боты
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")

    org = await get_organization_by_slug(slug, db)

    org_now = await get_org_now(db, org.id)

    start_time = to_salon_time(booking.start_time, org.timezone)
    await check_booking_horizon(db, org.id, start_time.date())

    if start_time <= org_now:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot book a time in the past")

    if booking.master_id is not None:
        service, master = await load_service_and_master(db, org.id, booking.service_id, booking.master_id)
        end_time = start_time + timedelta(minutes=service.duration_minutes)
        await check_slot_free(db, master.id, start_time, end_time)
        await check_on_slot_grid(db, master.id, start_time)
    else:
        # «Любой мастер»: из тех, у кого это время свободно, берём самого незагруженного в этот день
        service = await get_owned_active(db, models.Service, booking.service_id, org.id, "Service")
        end_time = start_time + timedelta(minutes=service.duration_minutes)
        free = []
        for candidate in await masters_for_service(db, org.id, service.id):
            try:
                await check_slot_free(db, candidate.id, start_time, end_time)
                await check_on_slot_grid(db, candidate.id, start_time)
            except HTTPException:
                continue
            free.append((await master_load(db, candidate.id, start_time.date()), candidate.id, candidate))
        if not free:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=NO_FREE_MASTER)
        master = min(free, key=lambda item: item[:2])[2]

    check_party_size(service, booking.party_size)

    # дальше проверка «не больше 3 будущих записей» и создание клиента: строго по одному запросу на телефон
    await lock_client_phone(db, org.id, booking.client_phone)

    client_result = await db.execute(
        select(models.Client).where(
            models.Client.organization_id == org.id,
            models.Client.phone == booking.client_phone,
            models.Client.deleted_at.is_(None),
        ),
    )
    client = client_result.scalars().first()

    if not client:
        client = models.Client(
            organization_id=org.id,
            full_name=booking.client_full_name,
            phone=booking.client_phone,
            email=booking.client_email,
        )
        db.add(client)
        await db.flush()

        await log_activity(
            db, org.id, None,
            action="created", entity_type="client", entity_id=client.id,
            details=f"Client created via public booking: {client.full_name}",
        )
    else:
        # защита от спама с одного номера: не больше PHONE_DAILY_LIMIT онлайн-записей за сутки (включая отменённые)
        recent = (await db.execute(
            select(func.count(models.Appointment.id)).where(
                models.Appointment.client_id == client.id,
                models.Appointment.booked_online.is_(True),
                models.Appointment.created_at >= utc_now() - timedelta(days=1),
            ),
        )).scalar()
        if recent >= PHONE_DAILY_LIMIT:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many online bookings from this phone number today. Please call the salon.",
            )
    # Существующую карточку из публичной формы НЕ меняем: номер телефона чужого клиента знает кто угодно,
    # и иначе можно было бы подставить свой email и получать письма и ссылки «отменить/перенести» по его записям.
    # Email из формы сохраняется только у этой записи (в её токене) и используется для писем по ней.

    active_count_result = await db.execute(
        select(models.Appointment).where(
            models.Appointment.client_id == client.id,
            models.Appointment.status == AppointmentStatus.scheduled,
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time > org_now,
        ),
    )
    if len(active_count_result.scalars().all()) >= 3:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You already have 3 upcoming appointments. Please complete or cancel one before booking another.",
        )

    new_appointment = await create_appointment_record(
        db,
        organization_id=org.id,
        client_id=client.id,
        service=service,
        master_id=master.id,
        start_time=start_time,
        end_time=end_time,
        notes=booking.notes,
        booked_online=True,
        party_size=booking.party_size,
        guest_names=booking.guest_names,
    )

    await log_activity(
        db, org.id, None,
        action="created", entity_type="appointment", entity_id=new_appointment.id,
        details=f"Public booking by {booking.client_full_name} ({booking.client_phone})",
    )

    await notify_booking(db, new_appointment, NotificationType.booking_created)  # колокольчик в CRM

    # письмо со ссылкой «отменить / перенести»: если клиент оставил email (сейчас или раньше)
    confirmation = None
    email_to = booking.client_email or client.email
    if email_to:
        manage_token = await create_token(db, new_appointment.id, booking.language, email_to)
        view = booking_view(
            organization=org, appointment_id=new_appointment.id, start_time=start_time, end_time=end_time,
            service_name=service.name, master_name=master.full_name, price=new_appointment.price,
            currency=new_appointment.currency, client_name=booking.client_full_name, language=booking.language,
        )
        confirmation = dict(
            to_email=email_to,
            organization_name=org.name,
            service_name=service.name,
            master_name=master.full_name,
            start_time=start_time,
            manage_token=manage_token,
            language=booking.language,
            party_size=booking.party_size,
            view=booking_links(view, manage_token),
        )
        confirmation_log = ("Failed to send booking confirmation (appointment_id=%s)", new_appointment.id)

    await db.commit()
    await db.refresh(new_appointment)
    if confirmation:
        background_tasks.add_task(safe_send, send_booking_confirmation_email, log=confirmation_log, **confirmation)
    return new_appointment


@router.get("/{slug}/available-dates", response_model=list[date_type])
@limiter.limit("30/minute")
async def get_available_dates(
    request: Request,
    slug: str,
    service_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    master_id: int | None = None,
    month: str | None = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$", description="YYYY-MM"),
    date_from: date_type | None = Query(default=None, alias="from", description="Первый день (вместо month)"),
    date_to: date_type | None = Query(default=None, alias="to", description="Последний день, не больше 31 дня"),
):
    """Дни, в которые есть свободное время. Либо month=YYYY-MM, либо from и to (до 31 дня, например неделя
    на стыке месяцев). В любом случае только от сегодня до сегодня + горизонт записи салона."""
    if month is not None:
        if date_from is not None or date_to is not None:
            raise HTTPException(status_code=422, detail="Pass either month or from/to, not both")
        year, month_num = map(int, month.split("-"))
        first_day = date_type(year, month_num, 1)
        if month_num == 12:
            last_day = date_type(year + 1, 1, 1) - timedelta(days=1)
        else:
            last_day = date_type(year, month_num + 1, 1) - timedelta(days=1)
    elif date_from is not None and date_to is not None:
        if date_to < date_from:
            raise HTTPException(status_code=422, detail="'to' must not be earlier than 'from'")
        if (date_to - date_from).days >= MAX_DATES_RANGE_DAYS:
            raise HTTPException(status_code=422, detail=f"The range must not exceed {MAX_DATES_RANGE_DAYS} days")
        first_day, last_day = date_from, date_to
    else:
        raise HTTPException(status_code=422, detail="Pass month=YYYY-MM or both from and to")

    org = await get_organization_by_slug(slug, db)
    if master_id is not None:
        service, _ = await load_service_and_master(db, org.id, service_id, master_id)
        master_ids = [master_id]
    else:  # «любой мастер»: день свободен, если свободен хоть один мастер, который делает услугу
        service = await get_owned_active(db, models.Service, service_id, org.id, "Service")
        master_ids = [m.id for m in await masters_for_service(db, org.id, service_id)]

    org_now = await get_org_now(db, org.id)
    today = org_now.date()
    max_date = today + timedelta(days=org.booking_horizon_days)

    available_dates = []
    current_date = max(first_day, today)
    end_date = min(last_day, max_date)

    while current_date <= end_date:
        for mid in master_ids:
            if await get_free_slots(db, mid, service.duration_minutes, current_date, org_now, first_only=True):
                available_dates.append(current_date)
                break
        current_date += timedelta(days=1)

    return available_dates


@router.get("/{slug}", response_model=PublicOrganizationInfo)
@limiter.limit("30/minute")
async def public_organization_info(request: Request, slug: str, db: Annotated[AsyncSession, Depends(get_db)]):
    org = await get_organization_by_slug(slug, db, require_booking=False)
    info = PublicOrganizationInfo.model_validate(org)
    info.booking_enabled = get_subscription(org).booking_enabled  # страница записи сама покажет «запись недоступна»
    info.group_bookings = org.group_bookings
    info.cancellation_hours = settings.booking_change_cutoff_hours
    info.opening_hours = await _opening_hours(db, org.id)
    return info


async def _opening_hours(db: AsyncSession, organization_id: int) -> list[OpeningHours]:
    """Часы работы салона: по каждому дню недели от самого раннего начала до самого позднего конца у мастеров."""
    rows = (await db.execute(
        select(models.WorkingHours.day_of_week, models.WorkingHours.start_time, models.WorkingHours.end_time)
        .join(models.Master, models.Master.id == models.WorkingHours.master_id)
        .where(models.Master.organization_id == organization_id, models.Master.deleted_at.is_(None)),
    )).all()
    days: dict[int, tuple[str, str]] = {}
    for day, start, end in rows:
        first, last = days.get(day, (start, end))
        days[day] = (min(first, start), max(last, end))
    return [OpeningHours(day_of_week=d, start_time=s, end_time=e) for d, (s, e) in sorted(days.items())]


# управление записью по ссылке из письма (/public/booking/...)
from routers.public_booking import router as _manage_router  # noqa: E402
from routers.public_games import router as _games_router  # noqa: E402

router.include_router(_manage_router)
router.include_router(_games_router)  # открытые игры: /public/{slug}/games..., /public/participation/...
