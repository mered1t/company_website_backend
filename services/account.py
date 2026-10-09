"""Удаление аккаунта пользователя (право на забвение, GDPR).

Удаляются: сам пользователь, его токены (вход, сброс пароля, подтверждение почты), его членства в организациях
и приглашения, отправленные на его email.
Остаются: сами организации и их данные (они принадлежат салонам, а не пользователю), записи журнала действий и
комментарии к клиентам: у них автор становится пустым. Имя пользователя и email из текста журнала заменяются на [deleted].

Владельца организации удалить нельзя: сначала нужно передать владение или удалить организацию.
Если добавляешь таблицу, которая ссылается на users, её нужно учесть здесь. Тест
test_every_foreign_key_to_users_is_handled напомнит об этом.
"""
import re

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models

REDACTED = "[deleted]"


class OwnsOrganizationError(Exception):
    """Пользователь владелец организации: сначала нужно передать владение или удалить организацию."""


def _redact(text: str, email: str, username: str) -> str:
    text = re.sub(re.escape(email), REDACTED, text, flags=re.IGNORECASE)
    # имя пользователя стоит в начале строки журнала: "<имя> accepted invitation as ..."
    return re.sub(rf"^{re.escape(username)}(?=\s)", REDACTED, text, flags=re.IGNORECASE)


async def _redact_activity_logs(db: AsyncSession, user_id: int, email: str, username: str) -> None:
    logs = (await db.execute(
        select(models.ActivityLog).where(
            or_(
                models.ActivityLog.details.icontains(email, autoescape=True),
                and_(models.ActivityLog.user_id == user_id,
                     models.ActivityLog.details.icontains(username, autoescape=True)),
            ),
        ),
    )).scalars().all()
    for log in logs:
        if log.user_id == user_id:
            log.details = _redact(log.details, email, username)
        else:
            # в чужой строке журнала убираем только email: имя того, кто её написал, не трогаем
            log.details = re.sub(re.escape(email), REDACTED, log.details, flags=re.IGNORECASE)


async def delete_user_account(db: AsyncSession, user: models.User) -> None:
    """Удалить пользователя. Коммит делает вызывающий код: всё происходит одной транзакцией."""
    user_id, email, username = user.id, user.email, user.username

    # Блокируем строки членств: параллельная передача владения этому пользователю не проскочит между проверкой и удалением
    memberships = (await db.execute(
        select(models.Membership).where(models.Membership.user_id == user_id).with_for_update(),
    )).scalars().all()
    if any(m.role == models.MembershipRole.owner for m in memberships):
        raise OwnsOrganizationError()

    await _redact_activity_logs(db, user_id, email, username)

    await db.execute(delete(models.Invitation).where(func.lower(models.Invitation.email) == email.lower()))
    for token_model in (models.RefreshToken, models.PasswordResetToken, models.EmailVerificationToken):
        await db.execute(delete(token_model).where(token_model.user_id == user_id))
    await db.execute(delete(models.Notification).where(models.Notification.user_id == user_id))
    await db.execute(delete(models.Membership).where(models.Membership.user_id == user_id))
    # ActivityLog, ClientComment и AiReport ссылаются на users с ON DELETE SET NULL: автор просто становится пустым
    await db.execute(delete(models.User).where(models.User.id == user_id))
