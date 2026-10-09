from datetime import datetime

from pydantic import BaseModel, computed_field

from domain.notifications import NotificationType


class NotificationPublic(BaseModel):
    """Уведомление для колокольчика. Тексты собирает фронтенд по `type` (так они переводятся на языки интерфейса).

    start_time и old_start_time: местное время салона без часового пояса, как у записей. created_at и read_at: UTC.
    """
    id: int
    type: NotificationType
    appointment_id: int | None
    client_name: str | None
    service_name: str
    master_name: str
    start_time: datetime
    old_start_time: datetime | None
    created_at: datetime
    read_at: datetime | None

    @computed_field
    @property
    def is_read(self) -> bool:
        return self.read_at is not None


class UnreadCount(BaseModel):
    unread: int


class ReadAllResult(BaseModel):
    updated: int
