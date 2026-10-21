import unicodedata
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from typing import Annotated, ClassVar, Literal
from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, field_validator, model_validator
from services.i18n import Language

import re

from domain.currencies import SUPPORTED_CURRENCIES, MAX_PRICE

from domain.enums import AppointmentStatus
from domain.business import BusinessType
from schemas.billing import SubscriptionInfo


def _validate_timezone(value: str | None) -> str | None:
    if value is None:
        return value
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Invalid timezone, use IANA name like 'Europe/Tirane'")
    return value


def _validate_currency(v: str) -> str:
    v = v.upper()
    if v not in SUPPORTED_CURRENCIES:
        raise ValueError(f"Unsupported currency, use one of: {', '.join(SUPPORTED_CURRENCIES)}")
    return v


SLUG_PATTERN = re.compile(r'^[a-z0-9]+(-[a-z0-9]+)*$')

# Имя: пробелы по краям убираются, пустая строка и "   " не проходят
Name150 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
GuestName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]

MAX_DURATION_MINUTES = 24 * 60
MAX_PASSWORD_LENGTH = 128


_EMOJI_CATEGORIES = {"So", "Sk", "Mn", "Me", "Cf"}  # символы, модификаторы тона, variation selector, ZWJ


def _validate_emoji(value: str | None) -> str | None:
    if value is None:
        return value
    if not value or len(value) > 16 or any(unicodedata.category(c) not in _EMOJI_CATEGORIES for c in value):
        raise ValueError("emoji must contain only emoji characters (up to 16 code points)")
    return value


def _validate_birth_date(value: date | None) -> date | None:
    if value is not None and not (date(1900, 1, 1) <= value <= date.today()):
        raise ValueError("birth_date must be between 1900-01-01 and today")
    return value

def _validate_slug(value: str) -> str:
    if not (3 <= len(value) <= 100):
        raise ValueError("Slug must be between 3 and 100 characters")
    if not SLUG_PATTERN.match(value):
        raise ValueError(
            "Slug can only contain lowercase latin letters, digits and single hyphens "
            "(no leading/trailing/double hyphens)"
        )
    return value


class PatchModel(BaseModel):
    """База для PATCH-схем: перечисленные в non_nullable поля нельзя явно обнулять (null).

    Если поле просто не передано, оно не меняется. Если передано как null, будет 422
    вместо ошибки базы данных.
    """
    non_nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _forbid_explicit_null(self):
        for name in self.model_fields_set & self.non_nullable:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class UserBase(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    email: EmailStr = Field(max_length=120)


class UserCreate(UserBase):
    password: str = Field(min_length=8, max_length=MAX_PASSWORD_LENGTH)
    accept_terms: bool
    language: Language = "en"

    @model_validator(mode="after")
    def check_password_strength(self) -> "UserCreate":
        if not any(c.isupper() for c in self.password):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in self.password):
            raise ValueError("Password must contain at least one digit")
        return self

    @model_validator(mode="after")
    def validate_terms_accepted(self):
        if not self.accept_terms:
            raise ValueError("You must accept the Terms of Service and Privacy Policy to register")
        return self


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str


class UserPrivate(UserPublic):
    email: EmailStr
    email_verified: bool
    terms_accepted_at: datetime | None = None
    language: str


class UserUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset({"username", "email", "language"})
    username: str | None = Field(default=None, min_length=1, max_length=50)
    email: EmailStr | None = Field(default=None, max_length=120)
    language: Language | None = None
    # нужен только для смены email: украденного токена входа не должно хватать, чтобы забрать аккаунт
    current_password: str | None = Field(default=None, max_length=MAX_PASSWORD_LENGTH)


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class RefreshRequest(BaseModel):
    refresh_token: str


class ClientBase(BaseModel):
    full_name: Name150
    phone: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    email: EmailStr | None = Field(default=None, max_length=120)
    birth_date: date | None = None
    notes: str | None = None


class ClientCreate(ClientBase):
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("birth_date")
    @classmethod
    def check_birth_date(cls, v):
        return _validate_birth_date(v)


class ClientUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset({"full_name", "phone"})
    full_name: Name150 | None = None
    phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")
    email: EmailStr | None = Field(default=None, max_length=120)
    birth_date: date | None = None
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("birth_date")
    @classmethod
    def check_birth_date(cls, v):
        return _validate_birth_date(v)


class ClientPublic(ClientBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    visits: int = 0  # сколько выполненных визитов
    last_visit: datetime | None = None  # начало последнего выполненного визита, местное время салона
    # сумма выполненных визитов в минимальных единицах валюты салона; мастеру не показывается (null)
    total_spent: int | None = 0
    no_shows: int = 0  # сколько раз не пришёл


class ClientsSummary(BaseModel):
    """Счётчики для шапки раздела «Клиенты» (по выполненным визитам)."""
    total: int
    new: int        # 0 визитов
    returning: int  # 1–2 визита
    regular: int    # 3 и больше


class ClientCommentCreate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ClientCommentPublic(BaseModel):
    id: int
    content: str
    author_username: str | None
    created_at: datetime


class ClientCommentUpdate(BaseModel):
    content: str = Field(min_length=1, max_length=2000)


class ClientImportError(BaseModel):
    row: int
    error: str


class ClientImportResult(BaseModel):
    created: int
    skipped_duplicates: int
    errors: list[ClientImportError]


MAX_PARTY_SIZE = 50  # человек в одной записи


class ServiceTranslation(BaseModel):
    """Название и описание услуги на одном языке."""
    name: Name150
    description: str | None = Field(default=None, max_length=2000)


class ServiceBase(BaseModel):
    name: Name150
    price: int = Field(ge=0, le=MAX_PRICE)
    duration_minutes: int = Field(gt=0)
    description: str | None = None
    photo: str | None = None
    emoji: str | None = None
    max_party_size: int = Field(default=1, ge=1, le=MAX_PARTY_SIZE)  # до скольких человек в одной записи
    # открытые игры: свободные места в записи могут занять другие люди (нужно max_party_size от 2)
    open_games: bool = False
    # переводы для публичной страницы записи по языкам интерфейса (en, pl, es, uk, sq); исходный текст — name/description
    translations: dict[Language, ServiceTranslation] = Field(default_factory=dict)


class ServiceCreate(ServiceBase):
    duration_minutes: int = Field(gt=0, le=MAX_DURATION_MINUTES)
    description: str | None = Field(default=None, max_length=2000)
    photo: str | None = Field(default=None, max_length=255)
    emoji: str | None = Field(default=None, max_length=16)

    @field_validator("emoji")
    @classmethod
    def check_emoji(cls, v):
        return _validate_emoji(v)


class ServiceUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset(
        {"name", "price", "duration_minutes", "max_party_size", "open_games", "translations"})
    name: Name150 | None = None
    translations: dict[Language, ServiceTranslation] | None = None  # заменяет все переводы; {} — удалить все
    max_party_size: int | None = Field(default=None, ge=1, le=MAX_PARTY_SIZE)
    open_games: bool | None = None
    price: int | None = Field(default=None, ge=0, le=MAX_PRICE)
    duration_minutes: int | None = Field(default=None, gt=0, le=MAX_DURATION_MINUTES)
    description: str | None = Field(default=None, max_length=2000)
    photo: str | None = Field(default=None, max_length=255)
    emoji: str | None = Field(default=None, max_length=16)

    @field_validator("emoji")
    @classmethod
    def check_emoji(cls, v):
        return _validate_emoji(v)


class ServicePublic(ServiceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class WorkingHoursFields(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")


class WorkingHoursBase(WorkingHoursFields):
    @model_validator(mode="after")
    def check_time_order(self) -> "WorkingHoursBase":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


class WorkingHoursPublic(WorkingHoursFields):
    model_config = ConfigDict(from_attributes=True)

    id: int


PHONE_PATTERN = r"^\+[1-9]\d{6,14}$"  # E.164


def _validate_url(value: str | None) -> str | None:
    """Ссылка для публичной страницы: только http(s), иначе в разметке мог бы оказаться javascript:..."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if not value.lower().startswith(("https://", "http://")) or any(c.isspace() for c in value):
        raise ValueError("Must be a link starting with https://")
    return value


class MasterBase(BaseModel):
    full_name: Name150
    phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")
    photo: str | None = None
    title: str | None = Field(default=None, max_length=100)  # должность: «Барбер», «Колорист»
    bio: str | None = Field(default=None, max_length=500)    # коротко о себе


class MasterCreate(MasterBase):
    photo: str | None = Field(default=None, max_length=255)
    working_hours: list[WorkingHoursBase] = Field(default=[], max_length=50)
    service_ids: list[int] = Field(default=[], max_length=200)


class MasterServicesUpdate(BaseModel):
    """Полный список услуг мастера (заменяет прежний). Пустой список = мастер делает все услуги салона."""
    service_ids: list[int] = Field(max_length=200)


class MasterUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset({"full_name"})
    full_name: Name150 | None = None
    phone: str | None = Field(default=None, pattern=r"^\+[1-9]\d{6,14}$")
    photo: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=100)
    bio: str | None = Field(default=None, max_length=500)


class MasterPublic(MasterBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    working_hours: list[WorkingHoursPublic] = []
    services: list[ServicePublic] = []


class MasterPublicInfo(BaseModel):
    """Мастер в публичном API записи: без телефона и служебных полей."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    full_name: str
    photo: str | None = None
    title: str | None = None
    bio: str | None = None
    working_hours: list[WorkingHoursPublic] = []
    services: list[ServicePublic] = []


class TimeOffCreate(BaseModel):
    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def check_dates(self) -> "TimeOffCreate":
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        return self


class TimeOffPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    start_date: date
    end_date: date
    reason: str | None


class WorkingHoursExceptionCreate(BaseModel):
    date: date
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")

    @model_validator(mode="after")
    def check_times(self) -> "WorkingHoursExceptionCreate":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


class WorkingHoursExceptionPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    date: date
    start_time: str
    end_time: str


class ConflictWarning(BaseModel):
    conflicting_appointment_ids: list[int]


class AppointmentBase(BaseModel):
    client_id: int
    service_id: int
    master_id: int
    start_time: datetime
    notes: str | None = None
    party_size: int = Field(default=1, ge=1, le=MAX_PARTY_SIZE)  # сколько человек придёт по этой записи
    guest_names: list[GuestName] = Field(default_factory=list, max_length=MAX_PARTY_SIZE - 1)
    # открытая игра: сколько всего людей может быть (свободные места занимают другие). None — запись закрытая
    open_capacity: int | None = Field(default=None, ge=2, le=MAX_PARTY_SIZE)


def _check_guest_count(guest_names: list[str], party_size: int) -> None:
    if len(guest_names) > party_size - 1:
        raise ValueError("guest_names: at most party_size - 1 names (the person who books is not listed)")


class AppointmentCreate(AppointmentBase):
    notes: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _guests_fit(self):
        _check_guest_count(self.guest_names, self.party_size)
        return self


class AppointmentUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset(
        {"client_id", "service_id", "master_id", "start_time", "status", "party_size", "guest_names"}
    )
    party_size: int | None = Field(default=None, ge=1, le=MAX_PARTY_SIZE)
    guest_names: list[GuestName] | None = Field(default=None, max_length=MAX_PARTY_SIZE - 1)
    # число мест открытой игры; null — закрыть игру (если никто ещё не присоединился)
    open_capacity: int | None = Field(default=None, ge=2, le=MAX_PARTY_SIZE)
    # false: не писать клиенту об этом изменении или отмене (салон уже договорился по телефону).
    # Не поле записи: на напоминание по новому времени не влияет.
    notify_client: bool = True

    client_id: int | None = None
    service_id: int | None = None
    master_id: int | None = None
    start_time: datetime | None = None
    status: AppointmentStatus | None = None
    notes: str | None = Field(default=None, max_length=1000)


class AppointmentPublic(AppointmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    end_time: datetime
    status: AppointmentStatus
    price: int
    currency: str
    series_id: str | None = None
    join_code: str | None = None  # открытая игра: код для ссылки /book/<slug>/game/<code>
    auto_completed: bool = False  # «выполнена» поставила автоотметка; салон может поправить на no_show
    created_at: datetime


MAX_SERIES_COUNT = 52  # записей в одной серии (год еженедельно)


class AppointmentSeriesCreate(AppointmentCreate):
    """Повторяющиеся записи: та же услуга, мастер и время каждые repeat_every_weeks недель, count раз."""
    repeat_every_weeks: int = Field(default=1, ge=1, le=4)
    count: int = Field(ge=2, le=MAX_SERIES_COUNT)
    skip_conflicts: bool = False  # True: занятые даты пропустить и создать остальные


class SeriesConflict(BaseModel):
    start_time: datetime
    reason: str


class AppointmentSeriesResult(BaseModel):
    series_id: str
    created: list[AppointmentPublic]
    skipped: list[SeriesConflict]


class SeriesCancelResult(BaseModel):
    cancelled: int

class AppointmentWithDetails(AppointmentPublic):
    model_config = ConfigDict(from_attributes=True)

    client_name: str
    service_name: str
    service_price: int
    master_name: str


class OrganizationCreate(BaseModel):
    name: Name150
    timezone: str | None = None
    currency: str | None = None
    # необязательный вопрос «чем вы занимаетесь?»: sports и studio сразу включают групповые записи
    business_type: BusinessType | None = None
    group_bookings: bool | None = None  # явно включить или выключить; не передано — по типу бизнеса

    @field_validator("currency")
    @classmethod
    def check_currency(cls, v):
        return _validate_currency(v) if v is not None else v

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, v):
        return _validate_timezone(v)


class OrganizationUpdate(PatchModel):
    non_nullable: ClassVar[frozenset[str]] = frozenset(
        {"name", "timezone", "booking_horizon_days", "slug", "currency",
         "staff_booking_reminders", "client_email_language", "group_bookings", "auto_complete"}
    )
    # автоотметка «Пришёл»: off — выключена, after_2h — через 2 часа после конца записи,
    # end_of_day — после конца дня записи по поясу салона (по умолчанию)
    auto_complete: Literal["off", "after_2h", "end_of_day"] | None = None
    staff_booking_reminders: bool | None = None  # напоминать клиентам о записях, созданных в CRM
    business_type: BusinessType | None = None  # null очищает
    group_bookings: bool | None = None  # групповые записи и открытые игры
    client_email_language: Language | None = None  # язык этих напоминаний

    # публичная страница записи; null очищает поле
    address: str | None = Field(default=None, max_length=300)
    phone: str | None = Field(default=None, pattern=PHONE_PATTERN)
    logo_url: str | None = Field(default=None, max_length=500)
    description: str | None = Field(default=None, max_length=1000)
    privacy_policy_url: str | None = Field(default=None, max_length=500)

    @field_validator("logo_url", "privacy_policy_url")
    @classmethod
    def validate_links(cls, v):
        return _validate_url(v)

    name: Name150 | None = None
    timezone: str | None = None
    booking_horizon_days: int | None = Field(default=None, ge=1, le=365)
    slug: str | None = None
    currency: str | None = None

    @field_validator("currency")
    @classmethod
    def validate_currency_field(cls, v):
        if v is None:
            raise ValueError("currency cannot be null")
        return _validate_currency(v)

    @field_validator("timezone")
    @classmethod
    def validate_tz(cls, v):
        return _validate_timezone(v) if v is not None else v

    @field_validator("slug")
    @classmethod
    def validate_slug_field(cls, v):
        return _validate_slug(v) if v is not None else v


class OrganizationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    slug: str
    created_at: datetime
    timezone: str
    booking_horizon_days: int
    currency: str
    plan: str
    staff_booking_reminders: bool = True
    auto_complete: str = "end_of_day"
    client_email_language: str = "en"
    business_type: BusinessType | None = None
    group_bookings: bool = False
    address: str | None = None
    phone: str | None = None
    logo_url: str | None = None
    description: str | None = None
    privacy_policy_url: str | None = None



class OpeningHours(BaseModel):
    day_of_week: int  # 0 = понедельник
    start_time: str
    end_time: str


class PublicOrganizationInfo(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    slug: str
    currency: str
    timezone: str
    booking_horizon_days: int
    booking_enabled: bool = True  # false, если у салона закончилась подписка: записываться нельзя
    group_bookings: bool = False  # true: показывать выбор числа людей и открытые игры
    address: str | None = None
    phone: str | None = None
    logo_url: str | None = None
    description: str | None = None
    privacy_policy_url: str | None = None
    # часы работы салона по дням недели: с самого раннего начала до самого позднего конца графиков мастеров
    opening_hours: list[OpeningHours] = []
    cancellation_hours: int = 0  # за сколько часов до начала клиент ещё может сам отменить или перенести запись


class OrganizationWithRole(OrganizationPublic):
    subscription: SubscriptionInfo
    role: str
    master_id: int | None = None


class MemberPublic(BaseModel):
    user_id: int
    username: str
    email: str
    role: str
    master_id: int | None = None


class InvitationCreate(BaseModel):
    email: EmailStr = Field(max_length=120)
    role: str = Field(pattern="^(admin|master)$")
    master_id: int | None = None
    language: Language | None = None


class InvitationPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    organization_id: int
    master_id: int | None
    expires_at: datetime
    accepted: bool


class InvitationPreview(BaseModel):
    organization_name: str
    email: str
    role: str
    valid: bool


class ForgotPasswordRequest(BaseModel):
    email: EmailStr


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8, max_length=MAX_PASSWORD_LENGTH)

    @model_validator(mode="after")
    def check_password_strength(self) -> "ResetPasswordRequest":
        if not any(c.isupper() for c in self.new_password):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in self.new_password):
            raise ValueError("Password must contain at least one digit")
        return self


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)
    new_password: str = Field(min_length=8, max_length=MAX_PASSWORD_LENGTH)

    @model_validator(mode="after")
    def check_password_strength(self) -> "ChangePasswordRequest":
        if not any(c.isupper() for c in self.new_password):
            raise ValueError("Password must contain at least one uppercase letter")
        if not any(c.isdigit() for c in self.new_password):
            raise ValueError("Password must contain at least one digit")
        return self


class VerifyEmailRequest(BaseModel):
    token: str


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class RevenueResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    total_revenue: int
    currency: str


class TopClientResponse(BaseModel):
    client_id: int
    full_name: str
    total_spent: int
    visits_count: int


class InactiveClientResponse(BaseModel):
    client_id: int
    full_name: str
    last_visit: datetime | None


class PopularServiceResponse(BaseModel):
    service_id: int
    name: str
    times_booked: int
    people: int  # сколько человек пришло по этим записям (запись компании считается по числу людей)
    total_revenue: int


class MasterWorkloadResponse(BaseModel):
    master_id: int
    full_name: str
    appointments_count: int  # выполненные записи
    no_show: int
    cancelled: int
    no_show_rate_percent: float
    people: int  # сколько человек пришло (запись компании считается по числу людей)
    total_revenue: int
    average_check: int
    booked_hours: float  # часы записей за период (все, кроме отменённых)
    available_hours: float  # часы по графику с учётом отгулов и особых часов
    load_percent: float  # booked_hours / available_hours; 0, если по графику часов нет


class StatusTrendPoint(BaseModel):
    period_start: datetime
    scheduled: int
    completed: int
    cancelled: int
    no_show: int
    total: int


class StatusTrendResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    group_by: str
    points: list[StatusTrendPoint]


class UpcomingBirthdayResponse(BaseModel):
    client_id: int
    full_name: str
    phone: str
    birth_date: date
    days_until: int
    turning_age: int


class RevenueTrendPoint(BaseModel):
    period_start: datetime
    revenue: int
    appointments: int


class RevenueTrendResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    group_by: str
    currency: str
    total_revenue: int
    previous_total_revenue: int
    change_percent: float | None
    points: list[RevenueTrendPoint]


class AppointmentsSummaryResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    currency: str
    total: int
    completed: int
    cancelled: int
    scheduled: int
    no_show: int
    people_served: int  # сколько человек пришло по выполненным записям; без записей компаний равно completed
    cancellation_rate_percent: float
    no_show_rate_percent: float
    average_check: int


class ClientsSummaryResponse(BaseModel):
    date_from: datetime
    date_to: datetime
    new_clients: int
    returning_clients: int
    total_clients: int
    returning_share_percent: float


class BusiestHourCell(BaseModel):
    weekday: int  # 0 = понедельник
    hour: int
    appointments: int


class AiReportRequest(BaseModel):
    date_from: date
    date_to: date
    language: Language | None = None  # по умолчанию язык пользователя


class AiReportResponse(BaseModel):
    id: int
    content: str
    language: str
    date_from: date
    date_to: date
    created_at: datetime
    cached: bool
    used_this_month: int
    monthly_limit: int


class AiUsageResponse(BaseModel):
    plan: str
    ai_enabled: bool
    used_this_month: int
    monthly_limit: int


class ActivityLogPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int | None
    action: str
    entity_type: str
    entity_id: int
    details: str | None
    created_at: datetime


class AvailableSlot(BaseModel):
    start_time: datetime
    end_time: datetime


class CrmAvailableSlot(AvailableSlot):
    master_ids: list[int]  # какие мастера свободны в это время


class PublicBookingRequest(BaseModel):
    client_full_name: Name150
    client_phone: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    client_email: EmailStr | None = Field(default=None, max_length=120)
    master_id: int | None = None  # не указан — «любой мастер»: салон сам назначит свободного мастера
    service_id: int
    start_time: datetime
    notes: str | None = Field(default=None, max_length=500)
    # Онлайн всегда записывается один человек. Компании, гостей и открытые игры создаёт только салон в CRM.
    # Поля оставлены, чтобы старый фронт получил понятную 422, а не молча записал одного вместо компании.
    party_size: int = Field(default=1, ge=1, le=1)
    guest_names: list[GuestName] = Field(default_factory=list, max_length=0)
    open_capacity: None = None
    language: Language | None = None  # язык, на котором клиент видит страницу записи: на нём придёт письмо
    # Поле-ловушка для ботов: на странице оно скрыто, человек его не заполняет. Заполнено — запрос отклоняется.
    website: str | None = Field(default=None, max_length=200)


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: int
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


class BookingManageInfo(BaseModel):
    """Запись глазами клиента, открывшего ссылку из письма. Персональных данных клиента здесь нет."""
    appointment_id: int
    status: AppointmentStatus
    start_time: datetime
    end_time: datetime
    service_id: int
    service_name: str
    master_id: int
    master_name: str
    price: int
    currency: str
    party_size: int
    open_capacity: int | None = None  # открытая игра: всего мест (party_size — сколько людей уже в игре)
    join_code: str | None = None      # открытая игра: код для ссылки «присоединиться»
    organization_name: str
    organization_slug: str
    can_modify: bool  # можно ли ещё отменить или перенести
    modify_deadline: datetime  # до какого момента можно отменить или перенести


class BookingRescheduleRequest(BaseModel):
    start_time: datetime


class AnalyticsDashboardResponse(BaseModel):
    """Всё для экрана аналитики одним запросом: те же данные, что у отдельных эндпоинтов."""
    summary: AppointmentsSummaryResponse
    revenue_trend: RevenueTrendResponse
    status_trend: StatusTrendResponse
    clients: ClientsSummaryResponse
    busiest_hours: list[BusiestHourCell]
    top_clients: list[TopClientResponse]
    popular_services: list[PopularServiceResponse]
    masters: list[MasterWorkloadResponse]


# ------------------------------------------------------------------ открытые игры
class OpenGamePublic(BaseModel):
    """Открытая игра на публичной странице салона. Имён и контактов игроков здесь нет — только сколько мест занято."""
    code: str
    service_id: int
    service_name: str
    master_id: int
    master_name: str
    start_time: datetime
    end_time: datetime
    capacity: int
    players: int
    spots_left: int
    price_per_person: int
    currency: str
    can_join: bool  # игра ещё не началась, не отменена и есть места


class JoinGameRequest(BaseModel):
    client_full_name: Name150
    client_phone: str = Field(pattern=r"^\+[1-9]\d{6,14}$")
    client_email: EmailStr | None = Field(default=None, max_length=120)
    party_size: int = Field(default=1, ge=1, le=1)  # онлайн входит один человек: компании записывает только салон
    language: Language | None = None
    website: str | None = Field(default=None, max_length=200)  # поле-ловушка для ботов


class GameSpotInfo(BaseModel):
    """Место участника глазами самого участника (страница по ссылке из письма)."""
    game: OpenGamePublic
    status: Literal["joined", "left", "cancelled"]  # cancelled — игру отменили
    party_size: int
    price: int
    organization_name: str
    organization_slug: str
    can_leave: bool
    leave_deadline: datetime


class ParticipantPublic(BaseModel):
    """Участник открытой игры для CRM."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    client_id: int
    client_name: str
    client_phone: str
    email: str | None
    party_size: int
    price: int
    created_at: datetime
    left_at: datetime | None


# ------------------------------------------------------------------ стартовая страница CRM «Сегодня» и счётчики меню
class TodaySummary(BaseModel):
    total: int       # записей сегодня (без отменённых)
    completed: int   # пришли
    no_show: int     # не пришли
    awaiting: int    # уже начались и всё ещё scheduled: ждут отметки
    upcoming: int    # ещё не начались
    cancelled: int   # отменены сегодня (в total не входят)
    earned: int      # сумма выполненных, минимальные единицы валюты салона
    expected: int    # сумма ещё не отмеченных (scheduled) — сколько ожидается


class UnmarkedBlock(BaseModel):
    total: int                              # всего неотмеченных записей прошлых дней (за UNMARKED_DAYS дней)
    items: list[AppointmentWithDetails]     # первые 50, старые сначала


class TomorrowBlock(BaseModel):
    count: int                              # записей завтра (без отменённых)
    items: list[AppointmentWithDetails]     # первые 50 по времени


class TodayResponse(BaseModel):
    date: date                               # сегодняшняя дата салона
    today: list[AppointmentWithDetails]      # все записи дня (включая отменённые), по времени
    summary: TodaySummary
    tomorrow: TomorrowBlock
    unmarked: UnmarkedBlock
    birthdays: list[UpcomingBirthdayResponse] | None  # сегодня; мастеру — null (дни рождения видят владелец и админ)


class MenuCounters(BaseModel):
    today: int                 # записей сегодня без отменённых (мастеру — свои)
    unmarked: int              # неотмеченных записей прошлых дней (как unmarked.total в /today)
    clients: int
    masters: int
    services: int
    waitlist: int | None       # заявки, которым письмо ещё не уходило; мастеру — null
    notifications_unread: int


class BulkStatusRequest(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=100)
    status: AppointmentStatus
    notify_client: bool = True  # как в PATCH: false — без писем клиентам об отмене


class BulkStatusFailure(BaseModel):
    id: int
    detail: str


class BulkStatusResult(BaseModel):
    updated: list[int]
    failed: list[BulkStatusFailure]


class ServiceTranslateRequest(BaseModel):
    """Черновик переводов услуги через ИИ (ничего не сохраняет: салон правит и сохраняет через PATCH)."""
    name: Name150
    description: str | None = Field(default=None, max_length=2000)
    source_language: Language | None = None   # на каком языке исходный текст; не указан — модель определит сама
    target_languages: list[Language] | None = None  # по умолчанию все языки интерфейса, кроме исходного


class ServiceTranslateResult(BaseModel):
    translations: dict[Language, ServiceTranslation]
