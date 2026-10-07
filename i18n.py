"""Языки интерфейса писем и тексты писем.

Албанский (sq) стоит показать носителю языка перед запуском.
В строках можно использовать {app_name} и {organization_name}.
"""
from typing import Literal

from markupsafe import Markup

Language = Literal["en", "pl", "es", "uk", "sq"]
SUPPORTED_LANGUAGES: tuple[str, ...] = ("en", "pl", "es", "uk", "sq")
DEFAULT_LANGUAGE = "en"

EMAIL_STRINGS: dict[str, dict[str, dict[str, str]]] = {
    "verify_email": {
        "en": {
            "subject": "Confirm your email",
            "heading": "Confirm your email",
            "body": "Hello! One last step: confirm your email address to use all features.",
            "button": "Confirm email",
            "fallback": "If the button doesn't work, copy this link:",
        },
        "pl": {
            "subject": "Potwierdź swój adres e-mail",
            "heading": "Potwierdź swój adres e-mail",
            "body": "Cześć! Został jeszcze jeden krok: potwierdź adres e-mail, aby korzystać ze wszystkich funkcji.",
            "button": "Potwierdź e-mail",
            "fallback": "Jeśli przycisk nie działa, skopiuj link:",
        },
        "es": {
            "subject": "Confirma tu correo electrónico",
            "heading": "Confirma tu correo electrónico",
            "body": "¡Hola! Solo falta confirmar tu correo electrónico para usar todas las funciones.",
            "button": "Confirmar correo",
            "fallback": "Si el botón no funciona, copia este enlace:",
        },
        "uk": {
            "subject": "Підтвердіть вашу електронну пошту",
            "heading": "Підтвердіть вашу електронну пошту",
            "body": "Вітаємо! Лишилося підтвердити електронну пошту, щоб користуватися всіма функціями.",
            "button": "Підтвердити пошту",
            "fallback": "Якщо кнопка не працює, скопіюйте посилання:",
        },
        "sq": {
            "subject": "Konfirmo email-in tënd",
            "heading": "Konfirmo email-in tënd",
            "body": "Përshëndetje! Mbetet vetëm të konfirmosh email-in për të përdorur të gjitha funksionet.",
            "button": "Konfirmo email-in",
            "fallback": "Nëse butoni nuk funksionon, kopjo lidhjen:",
        },
    },
    "password_reset": {
        "en": {
            "subject": "Reset your password",
            "heading": "Password reset",
            "body": "You requested a password reset for your account.",
            "button": "Set a new password",
            "footer": "This link is valid for 30 minutes. If you didn't request a reset, just ignore this email: your password will stay the same.",
        },
        "pl": {
            "subject": "Resetowanie hasła",
            "heading": "Resetowanie hasła",
            "body": "Poprosiłeś(-aś) o zresetowanie hasła do swojego konta.",
            "button": "Ustaw nowe hasło",
            "footer": "Link jest ważny przez 30 minut. Jeśli nie prosiłeś(-aś) o reset, po prostu zignoruj tę wiadomość: hasło pozostanie bez zmian.",
        },
        "es": {
            "subject": "Restablece tu contraseña",
            "heading": "Restablecimiento de contraseña",
            "body": "Has solicitado restablecer la contraseña de tu cuenta.",
            "button": "Crear una nueva contraseña",
            "footer": "El enlace es válido durante 30 minutos. Si no lo solicitaste, ignora este correo: tu contraseña no cambiará.",
        },
        "uk": {
            "subject": "Скидання пароля",
            "heading": "Скидання пароля",
            "body": "Ви надіслали запит на скидання пароля для вашого облікового запису.",
            "button": "Встановити новий пароль",
            "footer": "Посилання дійсне 30 хвилин. Якщо ви не надсилали запит, просто проігноруйте цей лист: пароль залишиться без змін.",
        },
        "sq": {
            "subject": "Rivendos fjalëkalimin",
            "heading": "Rivendosja e fjalëkalimit",
            "body": "Ke kërkuar rivendosjen e fjalëkalimit për llogarinë tënde.",
            "button": "Vendos fjalëkalim të ri",
            "footer": "Lidhja është e vlefshme për 30 minuta. Nëse nuk e ke kërkuar ti, thjesht injoroje këtë email: fjalëkalimi yt do të mbetet i njëjtë.",
        },
    },
    "invitation": {
        "en": {
            "subject": "You've been invited to join {organization_name}",
            "heading": "Team invitation",
            "body": "You've been invited to join <b>{organization_name}</b> on {app_name}.",
            "button": "Accept invitation",
            "footer": "This link is valid for 7 days. If you weren't expecting this email, just ignore it.",
        },
        "pl": {
            "subject": "Zaproszenie do zespołu {organization_name}",
            "heading": "Zaproszenie do zespołu",
            "body": "Zaproszono Cię do dołączenia do <b>{organization_name}</b> na platformie {app_name}.",
            "button": "Przyjmij zaproszenie",
            "footer": "Link jest ważny przez 7 dni. Jeśli nie spodziewałeś(-aś) się tej wiadomości, po prostu ją zignoruj.",
        },
        "es": {
            "subject": "Te han invitado a unirte a {organization_name}",
            "heading": "Invitación al equipo",
            "body": "Te han invitado a unirte a <b>{organization_name}</b> en {app_name}.",
            "button": "Aceptar invitación",
            "footer": "El enlace es válido durante 7 días. Si no esperabas este correo, simplemente ignóralo.",
        },
        "uk": {
            "subject": "Вас запрошено до {organization_name}",
            "heading": "Запрошення до команди",
            "body": "Вас запросили приєднатися до <b>{organization_name}</b> на платформі {app_name}.",
            "button": "Прийняти запрошення",
            "footer": "Посилання дійсне 7 днів. Якщо ви не очікували цей лист, просто проігноруйте його.",
        },
        "sq": {
            "subject": "Je ftuar të bashkohesh me {organization_name}",
            "heading": "Ftesë për ekipin",
            "body": "Je ftuar të bashkohesh me <b>{organization_name}</b> në platformën {app_name}.",
            "button": "Prano ftesën",
            "footer": "Lidhja është e vlefshme për 7 ditë. Nëse nuk e prisje këtë email, thjesht injoroje.",
        },
    },
    "booking_confirmation": {
        "en": {
            "subject": "Your appointment is confirmed",
            "heading": "Appointment confirmed",
            "body": "Your appointment at <b>{organization_name}</b> is confirmed.",
            "label_service": "Service",
            "label_master": "Specialist",
            "label_when": "Date and time",
            "button": "Manage appointment",
            "footer": "Plans changed? Use the button above to reschedule or cancel your appointment.",
            "fallback": "If the button doesn't work, copy this link:",
        },
        "pl": {
            "subject": "Twoja wizyta została potwierdzona",
            "heading": "Wizyta potwierdzona",
            "body": "Twoja wizyta w <b>{organization_name}</b> została potwierdzona.",
            "label_service": "Usługa",
            "label_master": "Specjalista",
            "label_when": "Data i godzina",
            "button": "Zarządzaj wizytą",
            "footer": "Plany się zmieniły? Użyj przycisku powyżej, aby przełożyć lub odwołać wizytę.",
            "fallback": "Jeśli przycisk nie działa, skopiuj link:",
        },
        "es": {
            "subject": "Tu cita está confirmada",
            "heading": "Cita confirmada",
            "body": "Tu cita en <b>{organization_name}</b> está confirmada.",
            "label_service": "Servicio",
            "label_master": "Profesional",
            "label_when": "Fecha y hora",
            "button": "Gestionar cita",
            "footer": "¿Han cambiado tus planes? Usa el botón de arriba para reprogramar o cancelar tu cita.",
            "fallback": "Si el botón no funciona, copia este enlace:",
        },
        "uk": {
            "subject": "Ваш запис підтверджено",
            "heading": "Запис підтверджено",
            "body": "Ваш запис у <b>{organization_name}</b> підтверджено.",
            "label_service": "Послуга",
            "label_master": "Майстер",
            "label_when": "Дата й час",
            "button": "Керувати записом",
            "footer": "Плани змінилися? Скористайтеся кнопкою вище, щоб перенести або скасувати запис.",
            "fallback": "Якщо кнопка не працює, скопіюйте посилання:",
        },
        "sq": {
            "subject": "Takimi yt është konfirmuar",
            "heading": "Takimi u konfirmua",
            "body": "Takimi yt te <b>{organization_name}</b> është konfirmuar.",
            "label_service": "Shërbimi",
            "label_master": "Specialisti",
            "label_when": "Data dhe ora",
            "button": "Menaxho takimin",
            "footer": "Planet kanë ndryshuar? Përdor butonin më sipër për ta shtyrë ose anuluar takimin.",
            "fallback": "Nëse butoni nuk funksionon, kopjo lidhjen:",
        },
    },
    "booking_reminder": {
        "en": {
            "subject": "Reminder: your appointment at {organization_name}",
            "heading": "Appointment reminder",
            "body": "This is a reminder about your upcoming appointment at <b>{organization_name}</b>.",
            "label_service": "Service",
            "label_master": "Specialist",
            "label_when": "Date and time",
            "button": "Manage appointment",
            "footer": "Can't make it? Please reschedule or cancel using the button above so someone else can take the slot.",
            "fallback": "If the button doesn't work, copy this link:",
        },
        "pl": {
            "subject": "Przypomnienie o wizycie w {organization_name}",
            "heading": "Przypomnienie o wizycie",
            "body": "Przypominamy o Twojej nadchodzącej wizycie w <b>{organization_name}</b>.",
            "label_service": "Usługa",
            "label_master": "Specjalista",
            "label_when": "Data i godzina",
            "button": "Zarządzaj wizytą",
            "footer": "Nie możesz przyjść? Przełóż lub odwołaj wizytę przyciskiem powyżej, aby ktoś inny mógł skorzystać z tego terminu.",
            "fallback": "Jeśli przycisk nie działa, skopiuj link:",
        },
        "es": {
            "subject": "Recordatorio de tu cita en {organization_name}",
            "heading": "Recordatorio de cita",
            "body": "Te recordamos tu próxima cita en <b>{organization_name}</b>.",
            "label_service": "Servicio",
            "label_master": "Profesional",
            "label_when": "Fecha y hora",
            "button": "Gestionar cita",
            "footer": "¿No puedes venir? Reprograma o cancela tu cita con el botón de arriba para que otra persona pueda usar ese horario.",
            "fallback": "Si el botón no funciona, copia este enlace:",
        },
        "uk": {
            "subject": "Нагадування про запис у {organization_name}",
            "heading": "Нагадування про запис",
            "body": "Нагадуємо про ваш найближчий запис у <b>{organization_name}</b>.",
            "label_service": "Послуга",
            "label_master": "Майстер",
            "label_when": "Дата й час",
            "button": "Керувати записом",
            "footer": "Не можете прийти? Перенесіть або скасуйте запис кнопкою вище, щоб цей час міг зайняти хтось інший.",
            "fallback": "Якщо кнопка не працює, скопіюйте посилання:",
        },
        "sq": {
            "subject": "Kujtesë për takimin tënd te {organization_name}",
            "heading": "Kujtesë për takimin",
            "body": "Të kujtojmë takimin tënd të radhës te <b>{organization_name}</b>.",
            "label_service": "Shërbimi",
            "label_master": "Specialisti",
            "label_when": "Data dhe ora",
            "button": "Menaxho takimin",
            "footer": "Nuk mund të vish? Shtyje ose anuloje takimin me butonin më sipër, që termini t'i shkojë dikujt tjetër.",
            "fallback": "Nëse butoni nuk funksionon, kopjo lidhjen:",
        },
    },
}


def normalize_language(language: str | None) -> str:
    """Неизвестный или пустой язык заменяется на английский."""
    return language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE


def email_texts(template_key: str, language: str | None, *, app_name: str, organization_name: str = "") -> dict[str, Markup]:
    """Тексты письма для шаблона. Подставляемые значения экранируются (Markup.format)."""
    strings = EMAIL_STRINGS[template_key][normalize_language(language)]
    return {
        key: Markup(value).format(app_name=app_name, organization_name=organization_name)
        for key, value in strings.items()
    }


def email_subject(template_key: str, language: str | None, *, organization_name: str = "") -> str:
    """Тема письма (обычный текст, без HTML)."""
    raw = EMAIL_STRINGS[template_key][normalize_language(language)]["subject"]
    return raw.format(organization_name=organization_name)
