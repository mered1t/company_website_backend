"""Тарифные планы. Тариф хранится в organizations.plan (строка, валидируется здесь).

Пока тариф выставляется вручную в БД, позже его будет менять оплата.
"""
from enum import StrEnum


class Plan(StrEnum):
    basic = "basic"
    pro = "pro"  # рабочее название второго тарифа


# какие тарифы включают ИИ-аналитику
AI_PLANS = frozenset({Plan.pro})


def plan_includes_ai(plan: str) -> bool:
    return plan in AI_PLANS
