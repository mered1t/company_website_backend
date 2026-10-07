"""Наблюдаемость и безопасность HTTP: номер запроса, журнал запросов без секретов, CORS, очистка событий Sentry."""
import logging
import re
import time
import uuid
from contextvars import ContextVar
from urllib.parse import urlsplit

import sentry_sdk
from starlette.datastructures import MutableHeaders

# номер текущего запроса: попадает в каждую строку лога, в заголовок ответа и в Sentry
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

access_logger = logging.getLogger("access")

_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")
# секретная ссылка записи лежит прямо в пути: /api/v1/public/booking/<токен>/cancel
_SECRET_IN_PATH = re.compile(r"(/booking/)[^/?#\s]+")


def mask_secrets(text: str) -> str:
    """Прячет секретные токены из путей, чтобы они не попадали в логи и в Sentry."""
    return _SECRET_IN_PATH.sub(r"\1{token}", text)


class RequestIdFilter(logging.Filter):
    """Добавляет к каждой записи лога поле request_id (формат: %(request_id)s)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class RequestIdMiddleware:
    """Выдаёт запросу номер, возвращает его в заголовке X-Request-ID и пишет одну строку в журнал запросов.

    Если клиент прислал свой X-Request-ID (8-64 символа: буквы, цифры, . _ -), используем его, иначе создаём новый.
    В журнал попадает путь без параметров запроса, а секретные токены заменены на {token}.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        context_token = request_id_var.set(request_id)
        sentry_sdk.set_tag("request_id", request_id)
        started = time.perf_counter()
        status_code = 500

        async def send_with_request_id(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            path = mask_secrets(scope.get("path", ""))  # без параметров запроса и без секретных токенов
            access_logger.info(
                "%s %s -> %s (%.0f ms)", scope.get("method", "-"), path, status_code,
                (time.perf_counter() - started) * 1000,
            )
            request_id_var.reset(context_token)


def scrub_sentry_event(event, hint):
    """before_send для Sentry: убирает секретные токены из адреса запроса."""
    request = event.get("request") or {}
    if request.get("url"):
        request["url"] = mask_secrets(request["url"])
    if event.get("transaction"):
        event["transaction"] = mask_secrets(event["transaction"])
    return event


def build_cors_origins(cors_origins: str, frontend_url: str) -> list[str]:
    """Список адресов, которым разрешено обращаться к API из браузера.

    Адрес фронтенда (FRONTEND_URL) разрешён всегда, остальные берутся из CORS_ORIGINS через запятую.
    Звёздочка не принимается: вместе с передачей авторизации это открыло бы API для любого сайта.
    """
    origins: list[str] = []
    for raw in [*cors_origins.split(","), frontend_url]:
        raw = raw.strip()
        parts = urlsplit(raw)
        if not (parts.scheme and parts.netloc):
            continue
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in origins:
            origins.append(origin)
    return origins
