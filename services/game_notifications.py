"""Письма об открытых играх: участникам (вошёл, напоминание, игру перенесли, отменили, убрали) и организатору
(кто-то присоединился или вышел). Готовятся до коммита, уходят в фоне после него (как booking_notifications)."""
from sqlalchemy.ext.asyncio import AsyncSession

import models
from services.booking_notifications import Notification, _recipient
from services.email_service import send_game_email
from services.email_view import booking_view, first_name, spot_links
from services.open_games import active_participants, game_url, spot_token, spot_url


async def _loaded(db: AsyncSession, appointment: models.Appointment):
    with db.no_autoflush:
        organization = await db.get(models.Organization, appointment.organization_id)
        service = await db.get(models.Service, appointment.service_id)
        master = await db.get(models.Master, appointment.master_id)
    return organization, service, master


def _view(organization, appointment, service, master, *, price: int, client_name: str | None, language: str) -> dict:
    return booking_view(
        organization=organization, appointment_id=appointment.id, start_time=appointment.start_time,
        end_time=appointment.end_time, service_name=service.name, master_name=master.full_name, price=price,
        currency=appointment.currency, client_name=client_name, language=language,
    )


async def participant_email(
    db: AsyncSession, appointment: models.Appointment, participant: models.AppointmentParticipant, kind: str,
) -> Notification | None:
    """Письмо одному участнику. None — писать некуда (нет адреса или клиент анонимизирован)."""
    with db.no_autoflush:
        client = await db.get(models.Client, participant.client_id)
    if client is None or client.anonymized_at is not None:
        return None
    email = participant.email or client.email
    if not email:
        return None
    organization, service, master = await _loaded(db, appointment)
    language = participant.language
    view = _view(organization, appointment, service, master, price=participant.price, client_name=client.full_name,
                 language=language)
    token = spot_token(participant)
    page = spot_url(token)
    if kind in ("joined", "reminder", "changed"):
        view = spot_links(view, token, page)
    else:
        view.pop("_calendar", None)
    return Notification(
        send=send_game_email,
        kwargs=dict(
            to_email=email, kind=kind, organization_name=organization.name, service_name=service.name,
            master_name=master.full_name, start_time=appointment.start_time, players=appointment.party_size,
            capacity=appointment.open_capacity or appointment.party_size,
            manage_url=page if kind in ("joined", "reminder", "changed") else None,
            language=language, view=view,
        ),
        log=("Failed to send game email %s (participant_id=%s)", kind, participant.id),
    )


async def participants_emails(db: AsyncSession, appointment: models.Appointment, kind: str) -> list[Notification]:
    """Письма всем, кто сейчас в игре (changed / cancelled). Вызывать до коммита."""
    if not appointment.open_capacity:
        return []
    notifications = []
    for participant in await active_participants(db, appointment.id):
        notification = await participant_email(db, appointment, participant, kind)
        if notification is not None:
            notifications.append(notification)
    return notifications


async def organizer_email(
    db: AsyncSession, appointment: models.Appointment, kind: str, player: models.Client,
) -> Notification | None:
    """Организатору: player_joined / player_left. Адрес и язык — как у остальных писем о его записи."""
    with db.no_autoflush:
        recipient = await _recipient(db, appointment)
    if recipient is None:
        return None
    email, language = recipient
    organization, service, master = await _loaded(db, appointment)
    view = _view(organization, appointment, service, master, price=appointment.price, client_name=None,
                 language=language)
    view.pop("_calendar", None)
    return Notification(
        send=send_game_email,
        kwargs=dict(
            to_email=email, kind=kind, organization_name=organization.name, service_name=service.name,
            master_name=master.full_name, start_time=appointment.start_time, players=appointment.party_size,
            capacity=appointment.open_capacity or appointment.party_size,
            manage_url=game_url(organization.slug, appointment.join_code) if appointment.join_code else None,
            player_name=first_name(player.full_name) if player.anonymized_at is None else None,
            language=language, view=view,
        ),
        log=("Failed to send game email %s (appointment_id=%s)", kind, appointment.id),
    )
