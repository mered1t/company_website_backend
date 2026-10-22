"""Уведомления внутри CRM («колокольчик»)."""
from enum import StrEnum

RETENTION_DAYS = 30  # уведомления старше этого срока удаляются


class NotificationType(StrEnum):
    booking_created = "booking_created"          # клиент записался онлайн
    booking_cancelled = "booking_cancelled"      # клиент сам отменил запись по ссылке из письма
    booking_rescheduled = "booking_rescheduled"  # клиент сам перенёс запись по ссылке из письма
    game_joined = "game_joined"                  # открытые игры убраны: новых нет, старые уведомления читаются
    game_left = "game_left"                      # открытые игры убраны: новых нет, старые уведомления читаются


# какой выключатель сотрудника отвечает за тип (у игр своих выключателей нет: «новая запись» и «отмена»)
SETTING_FOR_TYPE = {
    NotificationType.game_joined: "booking_created",
    NotificationType.game_left: "booking_cancelled",
}
