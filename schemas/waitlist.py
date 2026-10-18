from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from schemas.schemas import Name150
from services.i18n import Language


class WaitlistJoinRequest(BaseModel):
    service_id: int
    master_id: int | None = None  # не указан — подойдёт любой мастер, который делает услугу
    day: date
    client_full_name: Name150
    client_phone: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    client_email: EmailStr = Field(max_length=120)  # письмо — единственный способ сообщить, поэтому обязателен
    language: Language | None = None
    website: str | None = Field(default=None, max_length=200)  # ловушка для ботов, как в форме записи


class WaitlistJoinResult(BaseModel):
    id: int
    service_id: int
    master_id: int | None
    day: date


class WaitlistEntryPublic(BaseModel):
    """Заявка в листе ожидания для CRM."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    service_id: int
    master_id: int | None
    day: date
    client_name: str
    phone: str
    email: str
    language: str
    notified_at: datetime | None
    created_at: datetime
