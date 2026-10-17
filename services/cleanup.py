"""Чистка базы от строк, которые больше никому не нужны. Запускается раз в сутки из фонового цикла (services/reminders.py).

Что удаляется:
- refresh-токены: истёкшие и погашенные (выход, смена пароля, ротация) — через сутки;
- ссылки сброса пароля и подтверждения почты: использованные или истёкшие — через сутки;
- приглашения: через 30 дней после окончания срока (принятые тоже — сотрудник уже в команде);
- ссылки управления записью из писем: через 60 дней после визита;
- счётчик вопросов ИИ-чату: старше 400 дней (лимит считается за месяц, год оставляем для учёта расходов);
- журнал действий: старше ACTIVITY_LOG_RETENTION_DAYS дней (по умолчанию год; 0 — не удалять).

Не трогаются: клиенты, записи, комментарии, ИИ-отчёты, платежи. Уведомления чистятся отдельно (30 дней).
"""
from datetime import timedelta

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.config import settings
from core.time_utils import utc_now

CLEANUP_INTERVAL = timedelta(hours=24)
TOKEN_GRACE = timedelta(days=1)
INVITATION_KEEP = timedelta(days=30)
APPOINTMENT_LINK_KEEP = timedelta(days=60)
AI_CHAT_KEEP = timedelta(days=400)


async def _delete(db: AsyncSession, statement) -> int:
    result = await db.execute(statement.execution_options(synchronize_session=False))
    return result.rowcount or 0


async def purge_old_rows(db: AsyncSession) -> dict[str, int]:
    """Удаляет устаревшие строки одной транзакцией. Возвращает, сколько удалено по каждой таблице."""
    now = utc_now()
    token_edge = now - TOKEN_GRACE
    removed = {}

    removed["refresh_tokens"] = await _delete(db, delete(models.RefreshToken).where(or_(
        models.RefreshToken.expires_at < token_edge,
        models.RefreshToken.revoked.is_(True) & (models.RefreshToken.created_at < token_edge),
    )))
    for name, model in (("password_reset_tokens", models.PasswordResetToken),
                        ("email_verification_tokens", models.EmailVerificationToken)):
        removed[name] = await _delete(db, delete(model).where(or_(
            model.expires_at < token_edge,
            model.used.is_(True) & (model.created_at < token_edge),
        )))

    removed["invitations"] = await _delete(
        db, delete(models.Invitation).where(models.Invitation.expires_at < now - INVITATION_KEEP),
    )

    # время записи — местное время салона; разница в несколько часов при запасе в 60 дней не важна
    old_appointments = select(models.Appointment.id).where(
        models.Appointment.start_time < now - APPOINTMENT_LINK_KEEP,
    )
    removed["appointment_tokens"] = await _delete(
        db, delete(models.AppointmentToken).where(models.AppointmentToken.appointment_id.in_(old_appointments)),
    )

    removed["ai_chat_messages"] = await _delete(
        db, delete(models.AiChatMessage).where(models.AiChatMessage.created_at < now - AI_CHAT_KEEP),
    )

    removed["activity_logs"] = 0
    if settings.activity_log_retention_days > 0:
        edge = now - timedelta(days=settings.activity_log_retention_days)
        removed["activity_logs"] = await _delete(
            db, delete(models.ActivityLog).where(models.ActivityLog.created_at < edge),
        )

    await db.commit()
    return removed
