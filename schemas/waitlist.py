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
    """Заявка в листе ожидания для CRM (GET /organizations/{id}/waitlist)."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    service_id: int
    service_name: str = Field(description="Название услуги (у удалённой услуги — как было)")
    master_id: int | None = Field(description="null — подойдёт любой мастер")
    master_name: str | None = Field(description="null, если мастер не выбран")
    day: date = Field(description="На какой день клиент ждёт время")
    client_name: str = Field(description="Имя клиента из формы")
    phone: str = Field(description="Телефон клиента, E.164")
    email: str = Field(description="Email клиента: туда уходит письмо «освободилось время»")
    language: str = Field(description="Язык письма: en, pl, es, uk, sq")
    notified_at: datetime | None = Field(description="Когда ушло письмо (UTC); null — ещё не уходило")
    created_at: datetime = Field(description="Когда клиент записался в лист ожидания (UTC)")
