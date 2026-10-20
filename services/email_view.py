"""Данные записи для писем клиенту: дата по-человечески, цена, адрес салона, ссылки «в календарь».

Всё считается до отправки (письма уходят в фоне без базы), поэтому функции принимают уже загруженные объекты.
"""
from datetime import datetime
from urllib.parse import quote, urlencode

from core.config import settings
from domain.currencies import CURRENCIES
from services.i18n import MONTHS_SHORT, WEEKDAYS, normalize_language


def format_price(amount: int, currency: str) -> str:
    """1500, EUR → «€15»; 1550 → «€15.50». Цены хранятся в минимальных единицах."""
    info = CURRENCIES.get(currency, {"symbol": currency, "minor_unit": 2})
    value = amount / (10 ** info["minor_unit"])
    text = f"{value:.0f}" if value == int(value) else f"{value:.2f}"
    symbol = info["symbol"]
    return f"{symbol}{text}" if symbol in ("€", "$") else f"{text} {symbol}"


def first_name(full_name: str | None) -> str | None:
    if not full_name:
        return None
    return full_name.split()[0][:40]


def booking_view(
    *,
    organization,
    appointment_id: int,
    start_time: datetime,
    end_time: datetime,
    service_name: str,
    master_name: str,
    price: int,
    currency: str,
    client_name: str | None,
    language: str | None,
) -> dict:
    """Контекст для шаблонов писем о записи (без ссылок «в календарь»: их добавляет booking_links).

    Название салона, услугу, мастера, число людей и ссылку управления письмо получает отдельными аргументами.
    """
    lang = normalize_language(language)
    minutes = int((end_time - start_time).total_seconds() // 60)
    address = getattr(organization, "address", None)
    return {
        "organization_initial": (organization.name.strip()[:1] or "•").upper(),
        "logo_url": getattr(organization, "logo_url", None),
        "salon_address": address,
        "salon_phone": getattr(organization, "phone", None),
        "maps_url": f"https://www.google.com/maps/search/?api=1&query={quote(address)}" if address else None,
        "booking_number": appointment_id,
        "client_first_name": first_name(client_name),
        "month_short": MONTHS_SHORT[lang][start_time.month - 1],
        "day_number": start_time.day,
        "weekday": WEEKDAYS[lang][start_time.weekday()],
        "date_text": start_time.strftime("%d.%m.%Y"),
        "time_range": f"{start_time:%H:%M} – {end_time:%H:%M}",
        "start_hm": start_time.strftime("%H:%M"),
        "duration_minutes": minutes,
        "price_text": format_price(price, currency) if price else None,
        "cancel_hours": settings.booking_change_cutoff_hours,
        "book_again_url": f"{settings.frontend_url.rstrip('/')}/book/{organization.slug}",
        "_calendar": {  # для booking_links
            "title": f"{service_name} — {organization.name}",
            "start": start_time, "end": end_time, "timezone": organization.timezone,
            "location": address or "", "master": master_name,
        },
    }


def _with_calendar(view: dict, page_url: str, ics_path: str) -> dict:
    view = dict(view)
    cal = view.pop("_calendar", None)
    if cal:
        fmt = "%Y%m%dT%H%M%S"
        view["google_calendar_url"] = "https://calendar.google.com/calendar/render?" + urlencode({
            "action": "TEMPLATE", "text": cal["title"],
            "dates": f"{cal['start'].strftime(fmt)}/{cal['end'].strftime(fmt)}",
            "ctz": cal["timezone"], "location": cal["location"], "details": page_url,
        })
        if settings.backend_url:
            view["calendar_url"] = f"{settings.backend_url.rstrip('/')}/api/v1/public/{ics_path}"
    return view


def booking_links(view: dict, manage_token: str | None) -> dict:
    """Добавляет ссылки, которым нужен токен записи: управление и «в календарь»."""
    if not manage_token:
        view = dict(view)
        view.pop("_calendar", None)
        return view
    manage_url = f"{settings.frontend_url.rstrip('/')}/booking/{manage_token}"
    return _with_calendar(view, manage_url, f"booking/{manage_token}/calendar.ics")


def spot_links(view: dict, spot_token: str, spot_page: str) -> dict:
    """То же для участника открытой игры: его ссылка «выйти из игры» и свой файл .ics."""
    return _with_calendar(view, spot_page, f"participation/{spot_token}/calendar.ics")
