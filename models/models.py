from __future__ import annotations

from datetime import datetime

from sqlalchemy import (DDL,
                        DateTime,ForeignKey,
                        Integer,String, Text,
                        UniqueConstraint,
                        Index, event, CheckConstraint)

from enums import AppointmentStatus
from sqlalchemy.orm import Mapped, mapped_column, relationship
from enums import AppointmentStatus

from db.database import Base
from enum import Enum
from typing import Optional
from time_utils import utc_now

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    email: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified: Mapped[bool] = mapped_column(default=False, nullable=False)
    terms_accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    language: Mapped[str] = mapped_column(String(5), default="en", server_default="en", nullable=False)
    description: Mapped[str | None] = mapped_column(
        String(250),
        nullable=True,
        default=None,)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="user")


class Client(Base):
    __tablename__ = "clients"
    __table_args__ = (
        Index(
            "uq_client_phone_per_org_active",
            "organization_id", "phone",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    birth_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    anonymized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    organization: Mapped["Organization"] = relationship(back_populates="clients")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="client")


class ClientComment(Base):
    __tablename__ = "client_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    

class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True, default=None)
    photo: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    emoji: Mapped[str | None] = mapped_column(String(16), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    organization: Mapped["Organization"] = relationship(back_populates="services")


class Master(Base):
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(150), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    photo: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    organization: Mapped["Organization"] = relationship(back_populates="masters")
    working_hours: Mapped[list["WorkingHours"]] = relationship(back_populates="master", cascade="all, delete-orphan")
    time_off: Mapped[list["TimeOff"]] = relationship(back_populates="master", cascade="all, delete-orphan")
    working_hours_exceptions: Mapped[list["WorkingHoursException"]] = relationship(back_populates="master", cascade="all, delete-orphan")
    services: Mapped[list["Service"]] = relationship(secondary="master_services")


class MasterService(Base):
    __tablename__ = "master_services"
    __table_args__ = (UniqueConstraint("master_id", "service_id", name="uq_master_service"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id", ondelete="CASCADE"), nullable=False, index=True)


class WorkingHours(Base):
    __tablename__ = "working_hours"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False, index=True)
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = monday, 6 = sunday
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)  # "09:00"
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)    # "18:00"

    master: Mapped["Master"] = relationship(back_populates="working_hours")


class TimeOff(Base):
    __tablename__ = "time_off"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False, index=True)
    start_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    master: Mapped["Master"] = relationship(back_populates="time_off")


class WorkingHoursException(Base):
    __tablename__ = "working_hours_exceptions"
    __table_args__ = (UniqueConstraint("master_id", "date", "start_time", name="uq_master_exception_slot"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id", ondelete="CASCADE"), nullable=False, index=True)
    date: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    start_time: Mapped[str] = mapped_column(String(5), nullable=False)
    end_time: Mapped[str] = mapped_column(String(5), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    master: Mapped["Master"] = relationship(back_populates="working_hours_exceptions")


class Appointment(Base):
    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(
            "status IN ('scheduled', 'completed', 'cancelled', 'no_show')",
            name="ck_appointments_status",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), nullable=False, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"), nullable=False, index=True)
    start_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    end_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=AppointmentStatus.scheduled)
    price: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    # для какого времени начала уже отправлено напоминание. Если запись перенесли, значение перестаёт совпадать
    # с start_time, и напоминание уйдёт ещё раз, уже на новое время.
    reminded_start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)

    organization: Mapped["Organization"] = relationship(back_populates="appointments")
    client: Mapped["Client"] = relationship(back_populates="appointments")
    service: Mapped["Service"] = relationship()
    master: Mapped["Master"] = relationship()

    @property
    def service_name(self) -> str:
        return self.service.name

    @property
    def service_price(self) -> int:
        return self.price

    @property
    def master_name(self) -> str:
        return self.master.full_name

    @property
    def client_name(self) -> str:
        return self.client.full_name

    # Защита от двойной записи: один мастер не может иметь две пересекающиеся активные записи.
    # Правило создаётся вместе с таблицей (тесты), в боевой базе это делает миграция.
event.listen(
    Appointment.__table__,
    "after_create",
    DDL("CREATE EXTENSION IF NOT EXISTS btree_gist").execute_if(dialect="postgresql"),
)
event.listen(
    Appointment.__table__,
    "after_create",
    DDL(
        "ALTER TABLE appointments ADD CONSTRAINT appointments_no_master_overlap "
        "EXCLUDE USING gist (master_id WITH =, tsrange(start_time, end_time) WITH &&) "
        "WHERE (status <> 'cancelled' AND deleted_at IS NULL)"
    ).execute_if(dialect="postgresql"),
)


class MembershipRole(str, Enum):
    owner = "owner"
    admin = "admin"
    master = "master"


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    timezone: Mapped[str] = mapped_column(String(50), default="UTC", nullable=False)
    booking_horizon_days: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="EUR", nullable=False)
    plan: Mapped[str] = mapped_column(String(20), default="basic", server_default="basic", nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(back_populates="organization", cascade="all, delete-orphan")
    clients: Mapped[list["Client"]] = relationship(back_populates="organization")
    services: Mapped[list["Service"]] = relationship(back_populates="organization")
    masters: Mapped[list["Master"]] = relationship(back_populates="organization")
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="organization")
    invitations: Mapped[list["Invitation"]] = relationship(back_populates="organization", cascade="all, delete-orphan")


class Membership(Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("user_id", "organization_id", name="uq_user_organization"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    master_id: Mapped[int | None] = mapped_column(ForeignKey("masters.id", ondelete="SET NULL"), nullable=True)
    role: Mapped[MembershipRole] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    user: Mapped["User"] = relationship(back_populates="memberships")
    organization: Mapped["Organization"] = relationship(back_populates="memberships")
    master: Mapped[Optional["Master"]] = relationship()


class Invitation(Base):
    __tablename__ = "invitations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    master_id: Mapped[int | None] = mapped_column(ForeignKey("masters.id", ondelete="SET NULL"), nullable=True)
    email: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    role: Mapped[MembershipRole] = mapped_column(nullable=False)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    accepted: Mapped[bool] = mapped_column(default=False)
    language: Mapped[str] = mapped_column(String(5), default="en", server_default="en", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    organization: Mapped["Organization"] = relationship(back_populates="invitations")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    used: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


# Составные индексы и индексы на FK под частые запросы (миграция b7e41d2c9a10)
Index("ix_appointments_org_start", Appointment.organization_id, Appointment.start_time)
Index("ix_appointments_master_start", Appointment.master_id, Appointment.start_time)
Index("ix_activity_logs_org_created", ActivityLog.organization_id, ActivityLog.created_at, ActivityLog.id)
Index("ix_activity_logs_user_id", ActivityLog.user_id)
Index("ix_memberships_master_id", Membership.master_id)
Index("ix_invitations_master_id", Invitation.master_id)
Index("ix_client_comments_user_id", ClientComment.user_id)


class AiReport(Base):
    __tablename__ = "ai_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    organization_id: Mapped[int] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    period_start: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    period_end: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    language: Mapped[str] = mapped_column(String(5), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, default="pending")  # pending / done
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)


class AppointmentToken(Base):
    """Секретная ссылка из письма: по ней клиент без логина смотрит, отменяет и переносит свою запись.

    В базе лежит только хеш токена (как у приглашений): утечка базы не даёт доступ к записям.
    """
    __tablename__ = "appointment_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(5), default="en", server_default="en", nullable=False)
    # на какой адрес клиент оставил запись: туда же уйдёт напоминание
    email: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
