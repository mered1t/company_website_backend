"""Схемы подписки и админки платформы."""
from datetime import date, datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal

from domain.currencies import SUPPORTED_CURRENCIES
from domain.plans import Plan
from domain.subscription import Subscription, SubscriptionStatus, get_subscription

PaymentMethod = Literal["cash", "bank_transfer", "card", "grant"]
MAX_PAYMENT_AMOUNT = 100_000_000  # в минимальных единицах валюты (копейки, центы)


class SubscriptionInfo(BaseModel):
    """Состояние подписки для баннеров на фронтенде. Все даты в UTC без часового пояса."""
    status: SubscriptionStatus
    plan: str
    effective_plan: str
    ai_enabled: bool
    has_access: bool
    booking_enabled: bool
    trial_ends_at: datetime | None
    paid_until: datetime | None
    access_until: datetime | None
    grace_ends_at: datetime | None
    days_left: int | None

    @classmethod
    def from_subscription(cls, sub: Subscription) -> "SubscriptionInfo":
        return cls(**{name: getattr(sub, name) for name in cls.model_fields})

    @classmethod
    def from_org(cls, org, now: datetime | None = None) -> "SubscriptionInfo":
        return cls.from_subscription(get_subscription(org, now))


def _naive_utc(value: datetime | None) -> datetime | None:
    """В базе все даты в UTC без часового пояса: aware-значение переводим в UTC и отбрасываем зону."""
    if value is not None and value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class PaymentCreate(BaseModel):
    amount: int = Field(ge=0, le=MAX_PAYMENT_AMOUNT, description="В минимальных единицах валюты (центы, копейки)")
    currency: str = Field(min_length=3, max_length=3)
    method: PaymentMethod
    plan: Plan
    months: int = Field(ge=1, le=36)
    paid_at: date | None = None  # когда получены деньги; по умолчанию сегодня
    note: str | None = Field(default=None, max_length=500)

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v: str) -> str:
        v = v.upper()
        if v not in SUPPORTED_CURRENCIES:
            raise ValueError("Unsupported currency")
        return v

    @model_validator(mode="after")
    def check_amount_for_method(self):
        if self.method == "grant" and self.amount != 0:
            raise ValueError("A grant (free period) must have amount 0")
        if self.method != "grant" and self.amount == 0:
            raise ValueError("Amount must be greater than 0 (use method 'grant' for a free period)")
        return self


class PaymentPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    organization_id: int | None
    organization_name: str
    amount: int
    currency: str
    method: str
    plan: str
    months: int
    paid_at: datetime
    period_start: datetime
    period_end: datetime
    note: str | None
    recorded_by: int | None
    created_at: datetime


class AdminOrganizationUpdate(BaseModel):
    """Ручные правки админа платформы. Передавай только то, что нужно изменить."""
    plan: Plan | None = None
    is_free: bool | None = None
    is_blocked: bool | None = None
    billing_note: str | None = Field(default=None, max_length=1000)
    paid_until: datetime | None = None
    trial_ends_at: datetime | None = None

    @field_validator("paid_until", "trial_ends_at")
    @classmethod
    def to_naive_utc(cls, v):
        return _naive_utc(v)

    @model_validator(mode="after")
    def reject_null_for_required(self):
        # paid_until и billing_note можно сбросить в null, остальное нельзя
        for name in ("plan", "is_free", "is_blocked", "trial_ends_at"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class AdminOrganizationPublic(BaseModel):
    id: int
    name: str
    slug: str
    created_at: datetime
    owner_email: str | None
    plan: str
    is_free: bool
    is_blocked: bool
    billing_note: str | None
    subscription: SubscriptionInfo


class AdminOrganizationDetail(AdminOrganizationPublic):
    payments: list[PaymentPublic]


class PaymentResult(BaseModel):
    payment: PaymentPublic
    organization: AdminOrganizationPublic
