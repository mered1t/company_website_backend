from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from core.time_utils import utc_now
from db.database import Base


class Notification(Base):
    """Уведомление одному сотруднику («колокольчик» в CRM). На каждого получателя своя строка: «прочитано» у каждого своё.

    Хранится 30 дней (services/notifications.py). Имя клиента в строке не копируется, а берётся из карточки клиента
    при показе: после анонимизации клиента в уведомлении остаётся то, что осталось в карточке.
    """
    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    client_id: Mapped[int | None] = mapped_column(
        ForeignKey("clients.id", ondelete="SET NULL"), nullable=True, index=True,
    )
    service_name: Mapped[str] = mapped_column(String(150), nullable=False)
    master_name: Mapped[str] = mapped_column(String(150), nullable=False)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)  # местное время салона, как у записи
    old_start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # прежнее время при переносе
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
