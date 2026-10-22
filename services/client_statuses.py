"""Статусы клиентов: набор по умолчанию для нового салона. Дальше салон сам переименовывает, удаляет и добавляет."""
from sqlalchemy.ext.asyncio import AsyncSession

import models
from services.i18n import normalize_language

MAX_STATUSES = 30  # статусов в одном салоне

# порядок = порядок в списках; цвета — спокойные, читаются и в светлой, и в тёмной теме
DEFAULT_COLORS = ("#4C8DF6", "#2FA36B", "#C99A2E", "#8E8AA8", "#7A6FD6", "#D2555A", "#9AA0A6")
DEFAULT_NAMES: dict[str, tuple[str, ...]] = {
    "en": ("New", "Regular", "VIP", "One-time", "Sleeping", "Lost", "Buffer"),
    "pl": ("Nowy", "Stały", "VIP", "Jednorazowy", "Uśpiony", "Utracony", "Bufor"),
    "es": ("Nuevo", "Habitual", "VIP", "Ocasional", "Inactivo", "Perdido", "Búfer"),
    "uk": ("Новий", "Постійний", "VIP", "Разовий", "Сплячий", "Втрачений", "Буфер"),
    "sq": ("I ri", "I rregullt", "VIP", "Njëherësh", "Në gjumë", "I humbur", "Bufer"),
}


def default_statuses(organization_id: int, language: str | None) -> list[models.ClientStatus]:
    names = DEFAULT_NAMES[normalize_language(language)]
    return [
        models.ClientStatus(organization_id=organization_id, name=name, color=color, position=i)
        for i, (name, color) in enumerate(zip(names, DEFAULT_COLORS))
    ]


def add_default_statuses(db: AsyncSession, organization_id: int, language: str | None) -> None:
    """Добавляет набор по умолчанию в сессию (коммит делает вызывающий)."""
    db.add_all(default_statuses(organization_id, language))
