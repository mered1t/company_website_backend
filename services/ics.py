"""Файл .ics для кнопки «Добавить в календарь» (Apple Calendar, Outlook, Google — все понимают этот формат)."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import models


def _escape(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\;").replace(",", "\\,")
            .replace("\r", "").replace("\n", "\\n"))


def _fold(line: str) -> str:
    """Строки длиннее 75 байт переносятся (RFC 5545)."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, chunk = [], b""
    for char in line:
        encoded = char.encode()
        if len(chunk) + len(encoded) > (75 if not parts else 74):
            parts.append(chunk.decode())
            chunk = b""
        chunk += encoded
    parts.append(chunk.decode())
    return "\r\n ".join(parts)


def _utc(moment: datetime, tz_name: str) -> str:
    try:
        tz = ZoneInfo(tz_name)
    except Exception:  # noqa: BLE001 — неизвестная зона: считаем UTC
        tz = timezone.utc
    return moment.replace(tzinfo=tz).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def appointment_ics(appointment: models.Appointment, manage_url: str | None, now: datetime | None = None) -> str:
    """Время записи хранится в часовом поясе салона, в файл пишем UTC — календарь сам покажет местное."""
    org = appointment.organization
    title = f"{appointment.service.name} — {org.name}" if appointment.service else org.name
    details = [f"{appointment.master.full_name}"] if appointment.master else []
    if org.phone:
        details.append(org.phone)
    if manage_url:
        details.append(manage_url)
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    cancelled = appointment.status == "cancelled"
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Clientum//Booking//EN", "CALSCALE:GREGORIAN",
        "METHOD:CANCEL" if cancelled else "METHOD:PUBLISH",
        "BEGIN:VEVENT",
        f"UID:appointment-{appointment.id}@clientum",
        f"DTSTAMP:{stamp}",
        f"DTSTART:{_utc(appointment.start_time, org.timezone)}",
        f"DTEND:{_utc(appointment.end_time, org.timezone)}",
        f"SUMMARY:{_escape(title)}",
        f"DESCRIPTION:{_escape(chr(10).join(details))}",
    ]
    if org.address:
        lines.append(f"LOCATION:{_escape(org.address)}")
    if manage_url:
        lines.append(f"URL:{manage_url}")
    lines.append("STATUS:CANCELLED" if cancelled else "STATUS:CONFIRMED")
    if not cancelled:
        lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(title)}", "TRIGGER:-PT2H", "END:VALARM"]
    lines += ["END:VEVENT", "END:VCALENDAR"]
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"
