"""Открытые игры на публичной странице: список, страница игры, «присоединиться», место участника по ссылке из письма.

Подключены к public-роутеру: /public/{slug}/games..., /public/participation/{token}...
Логика мест и цен — services/open_games.py.
"""
from datetime import date as date_type, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from core.config import settings
from core.rate_limiter import limiter
from db.database import get_db
from domain.enums import AppointmentStatus
from domain.notifications import NotificationType
from schemas.schemas import GameSpotInfo, JoinGameRequest, OpenGamePublic
from services.billing import ensure_booking_enabled
from services.booking import lock_client_phone
from services.booking_notifications import dispatch
from services.common import get_org_now, log_activity
from services.game_notifications import organizer_email, participant_email
from services.i18n import normalize_language
from services.ics import appointment_ics
from services.notifications import notify_booking
from services.open_games import (add_participant, check_join_limits, load_game, load_spot, remove_participant,
                                 spot_token, spot_url, spots_left)

router = APIRouter()

MAX_GAMES_LISTED = 100


def _cutoff() -> timedelta:
    return timedelta(hours=settings.booking_change_cutoff_hours)


async def _organization(db: AsyncSession, slug: str, *, games: bool = False) -> models.Organization:
    """games=True: игры салона. Если групповые записи выключены, игр «нет» (404)."""
    from routers.public import get_organization_by_slug  # здесь, чтобы не было кругового импорта
    org = await get_organization_by_slug(slug, db)
    if games and not org.group_bookings:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Game not found")
    return org


async def _game_public(db: AsyncSession, appointment: models.Appointment, org_now: datetime) -> OpenGamePublic:
    with db.no_autoflush:
        service = await db.get(models.Service, appointment.service_id)
        master = await db.get(models.Master, appointment.master_id)
    left = spots_left(appointment)
    return OpenGamePublic(
        code=appointment.join_code, service_id=service.id, service_name=service.name, master_id=master.id,
        master_name=master.full_name, start_time=appointment.start_time, end_time=appointment.end_time,
        capacity=appointment.open_capacity, players=appointment.party_size, spots_left=left,
        price_per_person=service.price, currency=appointment.currency,
        can_join=(appointment.status == AppointmentStatus.scheduled and appointment.start_time > org_now
                  and left > 0 and service.open_games),
    )


@router.get("/{slug}/games", response_model=list[OpenGamePublic])
@limiter.limit("30/minute")
async def list_open_games(
    request: Request,
    slug: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    service_id: int | None = None,
    date: date_type | None = Query(default=None, description="Только игры этого дня"),
):
    """Будущие открытые игры салона, в которых есть свободные места (ближайшие первыми, не больше 100)."""
    org = await _organization(db, slug)
    if not org.group_bookings:
        return []
    org_now = await get_org_now(db, org.id)
    query = (
        select(models.Appointment)
        .join(models.Service, models.Service.id == models.Appointment.service_id)
        .where(
            models.Appointment.organization_id == org.id,
            models.Appointment.open_capacity.is_not(None),
            models.Appointment.party_size < models.Appointment.open_capacity,
            models.Appointment.status == AppointmentStatus.scheduled,
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time > org_now,
            models.Service.open_games.is_(True),
        )
        .order_by(models.Appointment.start_time)
        .limit(MAX_GAMES_LISTED)
    )
    if service_id is not None:
        query = query.where(models.Appointment.service_id == service_id)
    if date is not None:
        day = datetime.combine(date, datetime.min.time())
        query = query.where(models.Appointment.start_time >= day, models.Appointment.start_time < day + timedelta(days=1))
    return [await _game_public(db, a, org_now) for a in (await db.execute(query)).scalars().all()]


@router.get("/{slug}/games/{code}", response_model=OpenGamePublic)
@limiter.limit("30/minute")
async def get_open_game(request: Request, slug: str, code: str, db: Annotated[AsyncSession, Depends(get_db)]):
    """Страница игры по ссылке от организатора. Полная, прошедшая или отменённая игра тоже показывается (can_join=false)."""
    org = await _organization(db, slug, games=True)
    appointment = await load_game(db, org.id, code)
    return await _game_public(db, appointment, await get_org_now(db, org.id))


@router.post("/{slug}/games/{code}/join", response_model=OpenGamePublic, status_code=status.HTTP_201_CREATED)
@limiter.limit("10/hour")
async def join_open_game(
    request: Request,
    slug: str,
    code: str,
    body: JoinGameRequest,
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Присоединиться к игре: сразу, без одобрения. Письмо со ссылкой «выйти из игры» — если указан email."""
    if body.website:  # поле-ловушка для ботов
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid request")
    org = await _organization(db, slug, games=True)
    org_now = await get_org_now(db, org.id)

    await lock_client_phone(db, org.id, body.client_phone)
    appointment = await load_game(db, org.id, code, lock=True)  # строка игры заблокирована: места не «перебронируют»
    service = await db.get(models.Service, appointment.service_id)
    if (appointment.status != AppointmentStatus.scheduled or appointment.start_time <= org_now
            or not service.open_games):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This game is no longer open")

    client = (await db.execute(
        select(models.Client).where(
            models.Client.organization_id == org.id,
            models.Client.phone == body.client_phone,
            models.Client.deleted_at.is_(None),
        ),
    )).scalars().first()
    if client is None:
        client = models.Client(organization_id=org.id, full_name=body.client_full_name, phone=body.client_phone,
                               email=body.client_email)
        db.add(client)
        await db.flush()
        await log_activity(
            db, org.id, None, action="created", entity_type="client", entity_id=client.id,
            details=f"Client created by joining an open game: {client.full_name}",
        )
    else:
        await check_join_limits(db, appointment, client)

    language = normalize_language(body.language)
    participant = add_participant(db, appointment, service, client, party_size=body.party_size,
                                  email=body.client_email, language=language)
    await db.flush()
    await log_activity(
        db, org.id, None, action="updated", entity_type="appointment", entity_id=appointment.id,
        details=f"{body.client_full_name} ({body.client_phone}) joined the open game",
    )
    await notify_booking(db, appointment, NotificationType.game_joined, client_id=client.id)  # колокольчик
    # Карточку существующего клиента из публичной формы не меняем (как и при онлайн-записи); email формы — только
    # у этого места в игре.
    emails = [
        await participant_email(db, appointment, participant, "joined"),
        await organizer_email(db, appointment, "player_joined", client),
    ]
    result = await _game_public(db, appointment, org_now)
    await db.commit()
    for notification in emails:
        dispatch(background_tasks, notification)
    return result


# ------------------------------------------------------------------ место участника по ссылке из письма
async def _spot_info(db: AsyncSession, participant: models.AppointmentParticipant) -> GameSpotInfo:
    appointment = await db.get(models.Appointment, participant.appointment_id)
    organization = await db.get(models.Organization, appointment.organization_id)
    org_now = await get_org_now(db, organization.id)
    deadline = appointment.start_time - _cutoff()
    if participant.left_at is not None:
        state = "left"
    elif appointment.status == AppointmentStatus.cancelled or appointment.deleted_at is not None:
        state = "cancelled"
    else:
        state = "joined"
    game = await _game_public(db, appointment, org_now)
    return GameSpotInfo(
        game=game, status=state, party_size=participant.party_size, price=participant.price,
        organization_name=organization.name, organization_slug=organization.slug,
        can_leave=(state == "joined" and appointment.status == AppointmentStatus.scheduled and org_now < deadline),
        leave_deadline=deadline,
    )


@router.get("/participation/{token}", response_model=GameSpotInfo)
@limiter.limit("30/minute")
async def get_game_spot(request: Request, token: str, db: Annotated[AsyncSession, Depends(get_db)]):
    participant = await load_spot(db, token)
    return await _spot_info(db, participant)


@router.post("/participation/{token}/leave", response_model=GameSpotInfo)
@limiter.limit("10/hour")
async def leave_game(
    request: Request, token: str, background_tasks: BackgroundTasks, db: Annotated[AsyncSession, Depends(get_db)],
):
    """Выйти из игры: место освобождается, организатору уходит письмо. Не позже, чем за cutoff часов до начала."""
    participant = await load_spot(db, token)
    if not (await _spot_info(db, participant)).can_leave:
        if participant.left_at is not None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You have already left this game")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too late to leave the game online (up to {settings.booking_change_cutoff_hours} h before "
                   "the start). Please contact the salon.",
        )
    appointment = (await db.execute(
        select(models.Appointment).where(models.Appointment.id == participant.appointment_id)
        .with_for_update().execution_options(populate_existing=True),
    )).scalars().one()
    organization = await db.get(models.Organization, appointment.organization_id)
    ensure_booking_enabled(organization)
    remove_participant(appointment, participant)
    client = await db.get(models.Client, participant.client_id)
    await log_activity(
        db, appointment.organization_id, None, action="updated", entity_type="appointment", entity_id=appointment.id,
        details=f"Participant #{participant.id} left the open game",
    )
    await notify_booking(db, appointment, NotificationType.game_left, client_id=participant.client_id)
    notification = await organizer_email(db, appointment, "player_left", client)
    await db.commit()
    dispatch(background_tasks, notification)
    return await _spot_info(db, participant)


@router.get("/participation/{token}/calendar.ics", response_class=Response,
            responses={200: {"content": {"text/calendar": {}}}})
@limiter.limit("30/minute")
async def get_game_spot_calendar(request: Request, token: str, db: Annotated[AsyncSession, Depends(get_db)]):
    participant = await load_spot(db, token)
    appointment = (await db.execute(
        select(models.Appointment)
        .options(selectinload(models.Appointment.service), selectinload(models.Appointment.master),
                 selectinload(models.Appointment.organization))
        .where(models.Appointment.id == participant.appointment_id),
    )).scalars().one()
    if participant.left_at is not None:
        appointment.status = AppointmentStatus.cancelled  # только для файла: у участника игры больше нет
    appointment_id, participant_id = appointment.id, participant.id
    body = appointment_ics(appointment, spot_url(spot_token(participant)))
    await db.rollback()  # статус «отменено» выше — только для файла
    return Response(
        content=body.replace(f"UID:appointment-{appointment_id}@", f"UID:game-spot-{participant_id}@"),
        media_type="text/calendar; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="game-{appointment_id}.ics"',
                 "Cache-Control": "no-store"},
    )
