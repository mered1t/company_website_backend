"""Секретные ссылки на запись: создание токена и поиск записи по нему."""
import hashlib
import secrets

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

import models
from i18n import normalize_language


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_token(
    db: AsyncSession, appointment_id: int, language: str | None, email: str | None = None,
) -> str:
    """Добавляет токен в сессию (коммит делает вызывающий) и возвращает сырое значение для письма."""
    raw = secrets.token_urlsafe(32)
    db.add(models.AppointmentToken(
        appointment_id=appointment_id,
        token_hash=hash_token(raw),
        language=normalize_language(language),
        email=email,
    ))
    return raw


async def load_by_token(db: AsyncSession, raw: str) -> models.Appointment:
    """Запись по токену из ссылки. Для любого неверного токена один и тот же ответ 404."""
    result = await db.execute(
        select(models.Appointment)
        .join(models.AppointmentToken, models.AppointmentToken.appointment_id == models.Appointment.id)
        .options(
            selectinload(models.Appointment.service),
            selectinload(models.Appointment.master),
            selectinload(models.Appointment.organization),
        )
        .where(
            models.AppointmentToken.token_hash == hash_token(raw),
            models.Appointment.deleted_at.is_(None),
        ),
    )
    appointment = result.scalars().first()
    if not appointment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Booking not found")
    return appointment
