"""Записи «по людям»: у записи может быть несколько клиентов (основной + participants), и каждому нужна своя
история, свои визиты и свои траты. Подзапрос даёт по строке на каждого клиента каждой записи.

price в строке — часть этого клиента: у остальных клиентов — их price, у основного — сумма записи минус их части.
"""
from sqlalchemy import func, select, union_all

import models

A = models.Appointment
P = models.AppointmentParticipant


def person_appointments():
    """Подзапрос: client_id, appointment_id, organization_id, master_id, start_time, status, price, currency,
    deleted_at."""
    others = (
        select(func.coalesce(func.sum(P.price), 0))
        .where(P.appointment_id == A.id)
        .correlate(A)
        .scalar_subquery()
    )
    main = select(
        A.client_id.label("client_id"), A.id.label("appointment_id"), A.organization_id.label("organization_id"),
        A.master_id.label("master_id"), A.start_time.label("start_time"), A.status.label("status"),
        (A.price - others).label("price"), A.currency.label("currency"), A.deleted_at.label("deleted_at"),
    )
    extra = (
        select(P.client_id, A.id, A.organization_id, A.master_id, A.start_time, A.status, P.price, A.currency,
               A.deleted_at)
        .join(A, A.id == P.appointment_id)
    )
    return union_all(main, extra).subquery("person_appointments")


def client_appointment_ids(client_id: int):
    """id записей, где клиент — основной или один из остальных (для WHERE Appointment.id IN ...)."""
    return union_all(
        select(A.id).where(A.client_id == client_id),
        select(P.appointment_id).where(P.client_id == client_id),
    )
