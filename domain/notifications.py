"""Уведомления внутри CRM («колокольчик»)."""
from enum import StrEnum

RETENTION_DAYS = 30  # уведомления старше этого срока удаляются


class NotificationType(StrEnum):
    booking_created = "booking_created"          # клиент записался онлайн
    booking_cancelled = "booking_cancelled"      # клиент сам отменил запись по ссылке из письма
    booking_rescheduled = "booking_rescheduled"  # клиент сам перенёс запись по ссылке из письма
