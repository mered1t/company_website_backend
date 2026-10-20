"""Тип бизнеса салона и групповые функции.

Тип спрашивается (необязательно) при создании организации. Он только подсказывает, какие функции включить:
спорт и студии сразу получают групповые записи, салоны красоты и барбершопы — нет. Дальше всё решает
переключатель group_bookings в настройках организации.
"""
from enum import StrEnum


class BusinessType(StrEnum):
    beauty = "beauty"    # салон красоты, барбершоп, маникюр — один клиент на одно время
    sports = "sports"    # корты, спорт, тренеры — компании и открытые игры
    studio = "studio"    # студии, групповые занятия, мастер-классы
    other = "other"


GROUP_BY_DEFAULT = {BusinessType.sports, BusinessType.studio}

GROUP_BOOKINGS_OFF = "Group bookings are turned off for this organization. Turn them on in the organization settings"
