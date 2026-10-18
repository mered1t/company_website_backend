from datetime import timedelta
from datetime import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentMembership, CurrentUser
from auth.auth import ManagerMembership, OwnerMembership
from db.database import get_db
from schemas.schemas import AppointmentCreate, AppointmentPublic, AppointmentUpdate, AppointmentWithDetails

from services.booking import (
    check_master_provides_service, check_party_size, check_slot_free,
    create_appointment_record, persist,
)

from services.common import (get_owned, set_total_count,
                             get_owned_active,
                             log_activity,
                             restore_entity,
                             get_org_currency,
                             get_org_now)
from core.time_utils import utc_now
from domain.enums import AppointmentStatus
from services.booking_notifications import dispatch, prepare_cancelled_email, prepare_changed_email

router = APIRouter()


def _check_can_modify(membership: models.Membership, appointment: models.Appointment):
    if membership.role == models.MembershipRole.master and appointment.master_id != membership.master_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only modify your own appointments")


def _is_master(membership: models.Membership) -> bool:
    return membership.role == models.MembershipRole.master


def _visible_to(query, membership: models.Membership):
    """Мастер видит только свои записи (без цен и клиентов коллег); владелец и админ видят все."""
    if _is_master(membership):
        query = query.where(models.Appointment.master_id == membership.master_id)
    return query


class AppointmentFilters:
    """Фильтры списка и календаря. Каждый параметр можно повторить: ?master_id=1&master_id=2&status=scheduled."""

    def __init__(
        self,
        master_id: list[int] | None = Query(default=None, max_length=50),
        service_id: list[int] | None = Query(default=None, max_length=50),
        client_id: list[int] | None = Query(default=None, max_length=50),
        status: list[AppointmentStatus] | None = Query(default=None, max_length=4),
    ):
        self.master_id, self.service_id, self.client_id, self.status = master_id, service_id, client_id, status

    def apply(self, query):
        if self.master_id:
            query = query.where(models.Appointment.master_id.in_(self.master_id))
        if self.service_id:
            query = query.where(models.Appointment.service_id.in_(self.service_id))
        if self.client_id:
            query = query.where(models.Appointment.client_id.in_(self.client_id))
        if self.status:
            query = query.where(models.Appointment.status.in_([s.value for s in self.status]))
        return query


# поля, от которых зависят деньги и история визита: мастер правит их у закрытой записи только в день визита
_CLOSED_FIELDS = {"status", "service_id", "start_time", "client_id"}


@router.post("", response_model=AppointmentPublic, status_code=status.HTTP_201_CREATED)
async def create_appointment(
    appointment: AppointmentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    master_id = appointment.master_id
    if membership.role == models.MembershipRole.master:
        if membership.master_id is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Your membership is not linked to a master profile")
        master_id = membership.master_id

    await get_owned_active(db, models.Client, appointment.client_id, membership.organization_id, "Client")
    service = await get_owned_active(db, models.Service, appointment.service_id, membership.organization_id, "Service")
    await get_owned_active(db, models.Master, master_id, membership.organization_id, "Master")

    await check_master_provides_service(db, master_id, service.id)

    start_time = appointment.start_time.replace(tzinfo=None)
    end_time = start_time + timedelta(minutes=service.duration_minutes)
    await check_slot_free(db, master_id, start_time, end_time)

    new_appointment = await create_appointment_record(
        db,
        organization_id=membership.organization_id,
        client_id=appointment.client_id,
        service=service,
        master_id=master_id,
        start_time=start_time,
        end_time=end_time,
        notes=appointment.notes,
        party_size=appointment.party_size,
        guest_names=appointment.guest_names,
    )

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="appointment", entity_id=new_appointment.id,
    )

    await db.commit()
    await db.refresh(new_appointment)
    return new_appointment


@router.get("", response_model=list[AppointmentPublic])
async def list_appointments(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    filters: Annotated[AppointmentFilters, Depends()],
    date_from: dt | None = None,
    date_to: dt | None = None,
    direction: Literal["asc", "desc"] = Query(default="asc", alias="dir"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    query = select(models.Appointment).where(
        models.Appointment.organization_id == membership.organization_id,
        models.Appointment.deleted_at.is_(None),
    )
    if date_from is not None:
        query = query.where(models.Appointment.start_time >= date_from.replace(tzinfo=None))
    if date_to is not None:
        query = query.where(models.Appointment.start_time <= date_to.replace(tzinfo=None))
    query = _visible_to(filters.apply(query), membership)
    await set_total_count(response, db, query)

    if direction == "desc":
        query = query.order_by(models.Appointment.start_time.desc(), models.Appointment.id.desc())
    else:
        query = query.order_by(models.Appointment.start_time, models.Appointment.id)
    result = await db.execute(query.offset(skip).limit(limit))
    return result.scalars().all()


@router.get("/calendar", response_model=list[AppointmentWithDetails])
async def get_calendar(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    date_from: dt,
    date_to: dt,
    filters: Annotated[AppointmentFilters, Depends()],
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    # время в базе — местное время салона без часового пояса; дата с поясом («...+02:00») ломала сравнение (500)
    date_from, date_to = date_from.replace(tzinfo=None), date_to.replace(tzinfo=None)
    query = (
        select(models.Appointment)
        .options(
            selectinload(models.Appointment.client),
            selectinload(models.Appointment.service),
            selectinload(models.Appointment.master),
        )
        .where(
            models.Appointment.organization_id == membership.organization_id,
            models.Appointment.deleted_at.is_(None),
            models.Appointment.start_time >= date_from,
            models.Appointment.start_time <= date_to,
        )
    )
    query = _visible_to(filters.apply(query), membership)
    await set_total_count(response, db, query)

    query = query.order_by(models.Appointment.start_time, models.Appointment.id).offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{appointment_id}", response_model=AppointmentPublic)
async def get_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    query = select(models.Appointment).where(
        models.Appointment.id == appointment_id,
        models.Appointment.organization_id == membership.organization_id,
        models.Appointment.deleted_at.is_(None),
    )
    appointment = (await db.execute(_visible_to(query, membership))).scalars().first()
    if not appointment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    return appointment


@router.patch("/{appointment_id}", response_model=AppointmentPublic)
async def update_appointment(
    background_tasks: BackgroundTasks,
    appointment_id: int,
    appointment_update: AppointmentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    appointment = await get_owned_active(db, models.Appointment, appointment_id,
                                         membership.organization_id, "Appointment")
    _check_can_modify(membership, appointment)

    update_data = appointment_update.model_dump(exclude_unset=True)
    old_status, old_start = appointment.status, appointment.start_time
    old_master_id, old_service_id = appointment.master_id, appointment.service_id

    with db.no_autoflush:
        now_local = await get_org_now(db, membership.organization_id)
    # Закрытую запись (выполнена, отменена, не пришёл) мастер может поправить только в день визита,
    # дальше — владелец или админ. Иначе мастер мог бы задним числом «отменить» оплаченный визит и выручка исчезла бы.
    if (
        _is_master(membership)
        and old_status != AppointmentStatus.scheduled
        and old_start.date() < now_local.date()
        and _CLOSED_FIELDS & update_data.keys()
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner or an admin can change a closed appointment after its day",
        )

    # Проверяем, что переданные id принадлежат ЭТОЙ организации
    if "client_id" in update_data:
        await get_owned_active(db, models.Client, update_data["client_id"],
                               membership.organization_id, "Client")

    if "service_id" in update_data:
        await get_owned_active(db, models.Service, update_data["service_id"],
                               membership.organization_id, "Service")

    if "master_id" in update_data:
        if membership.role == models.MembershipRole.master:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Masters cannot reassign appointments to another master",
            )
        await get_owned_active(db, models.Master, update_data["master_id"],
                               membership.organization_id, "Master")

    recheck_needed = any(k in update_data for k in ("start_time", "master_id", "service_id"))

    new_service = None
    new_currency = None
    if recheck_needed:
        target_service_id = update_data.get("service_id", appointment.service_id)
        target_master_id = update_data.get("master_id", appointment.master_id)

        new_service = await get_owned_active(db, models.Service, target_service_id,
                                             membership.organization_id, "Service")
        await check_master_provides_service(db, target_master_id, new_service.id)

        if "service_id" in update_data:
            new_currency = await get_org_currency(db, membership.organization_id)

    # компания должна помещаться в услугу: и при смене числа людей, и при смене услуги
    if "party_size" in update_data or "service_id" in update_data:
        target_service = new_service or await get_owned_active(
            db, models.Service, appointment.service_id, membership.organization_id, "Service")
        check_party_size(target_service, update_data.get("party_size", appointment.party_size))

    if "party_size" in update_data or "guest_names" in update_data:
        guests = update_data.get("guest_names", appointment.guest_names) or []
        if len(guests) > update_data.get("party_size", appointment.party_size) - 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Too many guest names for this party size",
            )

    for field, value in update_data.items():
        setattr(appointment, field, value)

    if appointment.start_time.tzinfo is not None:
        appointment.start_time = appointment.start_time.replace(tzinfo=None)

    # «Выполнена» мастер ставит только начавшейся записи; владелец и админ могут закрыть день заранее
    if (
        _is_master(membership)
        and appointment.status == AppointmentStatus.completed
        and ("status" in update_data or "start_time" in update_data)
        and appointment.start_time > now_local
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An appointment can be marked completed only after it has started",
        )

    # «Не пришёл» можно поставить, когда запись уже началась: во время приёма или после него
    if appointment.status == AppointmentStatus.no_show and ("status" in update_data or "start_time" in update_data):
        with db.no_autoflush:  # правки записи ещё не сохранены: запрос времени не должен отправлять их в базу раньше срока
            org_now = await get_org_now(db, membership.organization_id)
        if appointment.start_time > org_now:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="A no-show can only be set once the appointment has started. "
                       "To drop a future appointment, cancel it instead.",
            )


    if recheck_needed:
        appointment.end_time = appointment.start_time + timedelta(minutes=new_service.duration_minutes)
        if "service_id" in update_data:
            appointment.price = new_service.price
            appointment.currency = new_currency

        await check_slot_free(db, appointment.master_id, appointment.start_time,
                                  appointment.end_time, exclude_id=appointment.id)

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="updated", entity_type="appointment", entity_id=appointment_id,
        details=f"Updated fields: {', '.join(update_data.keys())}",
    )

    # Письмо клиенту: будущую запись отменили или поменяли время, мастера, услугу.
    # Прошедшие записи и правка заметок/статуса письма не вызывают.
    notification = None
    if old_status == AppointmentStatus.scheduled:
        with db.no_autoflush:
            org_now = await get_org_now(db, membership.organization_id)
        if appointment.status == AppointmentStatus.cancelled and old_start > org_now:
            notification = await prepare_cancelled_email(db, appointment, by_salon=True)
        elif (
            appointment.status == AppointmentStatus.scheduled
            and appointment.start_time > org_now
            and (appointment.start_time != old_start
                 or appointment.master_id != old_master_id
                 or appointment.service_id != old_service_id)
        ):
            notification = await prepare_changed_email(
                db, appointment,
                old_start=old_start if appointment.start_time != old_start else None,
                by_salon=True,
            )

    await persist(db, commit=True)
    await db.refresh(appointment)
    dispatch(background_tasks, notification)
    return appointment


@router.post("/{appointment_id}/restore", response_model=AppointmentPublic)
async def restore_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: ManagerMembership,
):
    appointment = await restore_entity(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")

    # запись нельзя вернуть к удалённому мастеру, услуге или клиенту: сначала восстановите их
    with db.no_autoflush:
        for model, field, label in (
            (models.Master, appointment.master_id, "master"),
            (models.Service, appointment.service_id, "service"),
            (models.Client, appointment.client_id, "client"),
        ):
            related = await db.get(model, field)
            if related is None or related.deleted_at is not None:
                await db.rollback()
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Cannot restore: the {label} of this appointment is deleted. Restore the {label} first",
                )

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="restored", entity_type="appointment", entity_id=appointment_id,
        details="Restored appointment",
    )

    await persist(db, commit=True,
                  detail="Cannot restore: the master already has an appointment at this time")
    await db.refresh(appointment)
    return appointment


@router.delete("/{appointment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    appointment = await get_owned(db, models.Appointment, appointment_id, membership.organization_id, "Appointment")
    if membership.role == models.MembershipRole.master:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Masters cannot delete appointments")

    appointment.deleted_at = utc_now()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="appointment", entity_id=appointment_id,
    )

    await db.commit()


@router.delete("/{appointment_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def hard_delete_appointment(
    appointment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: OwnerMembership,
):
    result = await db.execute(
        select(models.Appointment).where(
            models.Appointment.id == appointment_id,
            models.Appointment.organization_id == membership.organization_id,
        ),
    )
    appointment = result.scalars().first()
    if not appointment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Appointment not found")
    if appointment.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Appointment must be soft-deleted first")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="hard_deleted", entity_type="appointment", entity_id=appointment_id,
        details="Permanently deleted appointment",
    )

    await db.delete(appointment)
    await db.commit()