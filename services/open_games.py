"""Открытые игры: в записи есть свободные места, и их могут занять другие люди (теннис 2–4, падел, групповая тренировка).

Как устроено:
  * Организатор (обычная запись: онлайн или из CRM) делает запись открытой: open_capacity — сколько всего людей.
    Нужна услуга с open_games = true; open_capacity не больше max_party_size услуги.
  * У игры есть публичный код join_code: страница игры /book/<slug>/game/<code> и список открытых игр салона.
  * Присоединиться может кто угодно, без одобрения: своя карточка клиента, своя ссылка «выйти из игры» в письме.
  * Цена в открытой игре — за человека: price услуги × число людей. У записи party_size и price — итог по всем.
  * Если организатор или салон отменили или перенесли игру, участникам уходит письмо. Если людей не набралось,
    игра всё равно остаётся: решает организатор.
"""
import base64
import hashlib
import hmac
import secrets
from datetime import timedelta

from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.config import settings
from services.booking import ensure_group_bookings
from core.time_utils import utc_now

GAME_PAGE_PATH = "/book/{slug}/game/{code}"   # страница игры на фронтенде (присоединиться)
SPOT_PAGE_PATH = "/game/{token}"              # страница участника (выйти из игры)
JOIN_DAILY_LIMIT = 5                          # присоединений с одного номера в салоне за сутки


def _frontend(path: str) -> str:
    return settings.frontend_url.rstrip("/") + path


def game_url(slug: str, code: str) -> str:
    return _frontend(GAME_PAGE_PATH.format(slug=slug, code=code))


def spot_url(token: str) -> str:
    return _frontend(SPOT_PAGE_PATH.format(token=token))


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def spot_token(participant: models.AppointmentParticipant) -> str:
    """Сырой токен ссылки участника: всегда один и тот же для этого места, вычисляется из секретного ключа."""
    digest = hmac.new(settings.secret_key.get_secret_value().encode(), f"spot:{participant.token_nonce}".encode(),
                      hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def new_join_code() -> str:
    return secrets.token_urlsafe(9)  # 12 символов, угадать нельзя


def spots_left(appointment: models.Appointment) -> int:
    if not appointment.open_capacity:
        return 0
    return max(appointment.open_capacity - appointment.party_size, 0)


async def joined_seats(db: AsyncSession, appointment_id: int) -> int:
    """Сколько мест заняли присоединившиеся (без организатора и его компании)."""
    with db.no_autoflush:
        return (await db.execute(
            select(func.coalesce(func.sum(models.AppointmentParticipant.party_size), 0)).where(
                models.AppointmentParticipant.appointment_id == appointment_id,
                models.AppointmentParticipant.left_at.is_(None),
            ),
        )).scalar_one()


async def active_participants(db: AsyncSession, appointment_id: int) -> list[models.AppointmentParticipant]:
    with db.no_autoflush:
        return list((await db.execute(
            select(models.AppointmentParticipant)
            .where(models.AppointmentParticipant.appointment_id == appointment_id,
                   models.AppointmentParticipant.left_at.is_(None))
            .order_by(models.AppointmentParticipant.id),
        )).scalars().all())


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def set_open_capacity(db: AsyncSession, appointment: models.Appointment, service: models.Service,
                            capacity: int | None, joined: int) -> None:
    """Открыть игру (capacity), поменять число мест или закрыть (None). joined — места присоединившихся."""
    if capacity is None:
        if joined:
            raise _bad("Players have already joined this game. Remove them before closing the game")
        appointment.open_capacity = None
        appointment.join_code = None
        appointment.price = service.price
        return
    await ensure_group_bookings(db, appointment.organization_id)
    if not service.open_games:
        raise _bad("This service does not allow open games")
    if capacity > service.max_party_size:
        raise _bad(f"This service allows at most {service.max_party_size} people per booking")
    if capacity < appointment.party_size:
        raise _bad("open_capacity cannot be less than the number of people already in the booking")
    appointment.open_capacity = capacity
    appointment.join_code = appointment.join_code or new_join_code()
    appointment.price = service.price * appointment.party_size


async def recalc_open_game(db: AsyncSession, appointment: models.Appointment, service: models.Service) -> None:
    """Открытая игра после правки в CRM: люди помещаются, цены пересчитаны по цене услуги за человека."""
    participants = await active_participants(db, appointment.id)
    joined = sum(p.party_size for p in participants)
    if appointment.party_size < joined + 1:
        raise _bad(f"{joined} people have joined this game: party_size must be at least {joined + 1}")
    if not service.open_games:
        raise _bad("This service does not allow open games")
    if appointment.party_size > appointment.open_capacity:
        raise _bad("party_size cannot be greater than open_capacity")
    if appointment.open_capacity > service.max_party_size:
        raise _bad(f"This service allows at most {service.max_party_size} people per booking")
    for participant in participants:
        participant.price = service.price * participant.party_size
    appointment.price = service.price * appointment.party_size


async def load_game(db: AsyncSession, organization_id: int, code: str, *, lock: bool = False) -> models.Appointment:
    """Игра по публичному коду. 404 — нет такой (или её закрыли, удалили)."""
    query = select(models.Appointment).where(
        models.Appointment.organization_id == organization_id,
        models.Appointment.join_code == code,
        models.Appointment.open_capacity.is_not(None),
        models.Appointment.deleted_at.is_(None),
    )
    if lock:
        query = query.with_for_update()
    appointment = (await db.execute(query)).scalars().first()
    if appointment is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Game not found")
    return appointment


async def load_spot(db: AsyncSession, raw: str) -> models.AppointmentParticipant:
    """Место участника по токену из письма. 404 для любого неверного токена."""
    participant = (await db.execute(
        select(models.AppointmentParticipant).where(models.AppointmentParticipant.token_hash == hash_token(raw)),
    )).scalars().first()
    if participant is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not found")
    return participant


async def check_join_limits(db: AsyncSession, appointment: models.Appointment, client: models.Client) -> None:
    if client.id == appointment.client_id:
        raise _bad("You are already in this game")
    already = (await db.execute(
        select(models.AppointmentParticipant.id).where(
            models.AppointmentParticipant.appointment_id == appointment.id,
            models.AppointmentParticipant.client_id == client.id,
            models.AppointmentParticipant.left_at.is_(None),
        ),
    )).first()
    if already:
        raise _bad("You are already in this game")
    recent = (await db.execute(
        select(func.count(models.AppointmentParticipant.id)).where(
            models.AppointmentParticipant.client_id == client.id,
            models.AppointmentParticipant.created_at >= utc_now() - timedelta(days=1),
        ),
    )).scalar_one()
    if recent >= JOIN_DAILY_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many games joined from this phone number today. Please call the salon.",
        )


def add_participant(db: AsyncSession, appointment: models.Appointment, service: models.Service, client: models.Client,
                    *, party_size: int, email: str | None, language: str) -> models.AppointmentParticipant:
    """Новый участник (без коммита). Ссылку «выйти из игры» даёт spot_token(participant)."""
    if party_size > spots_left(appointment):
        raise _bad("Not enough free spots in this game")
    participant = models.AppointmentParticipant(
        organization_id=appointment.organization_id, appointment_id=appointment.id, client_id=client.id,
        party_size=party_size, price=service.price * party_size, email=email, language=language,
        token_nonce=secrets.token_hex(16),
    )
    participant.token_hash = hash_token(spot_token(participant))
    db.add(participant)
    appointment.party_size += party_size
    appointment.price += participant.price
    return participant


def remove_participant(appointment: models.Appointment, participant: models.AppointmentParticipant) -> None:
    """Участник вышел или его убрал салон: место снова свободно (без коммита)."""
    participant.left_at = utc_now()
    appointment.party_size = max(appointment.party_size - participant.party_size, 1)
    appointment.price = max(appointment.price - participant.price, 0)
