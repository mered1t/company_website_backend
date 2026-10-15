from fastapi.middleware.cors import CORSMiddleware
from fastapi import APIRouter, FastAPI, Depends, HTTPException
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import asyncio
import secrets
from contextlib import asynccontextmanager, suppress

from db.database import AsyncSessionLocal, engine, get_db
from services.reminders import reminder_loop
from routers import users, clients, services, masters, appointments, analytics, organizations, invitations, public, admin
from routers import notifications
from routers import ai_chat

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from core.rate_limiter import limiter

from sqlalchemy import text
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

import sentry_sdk
import logging
from core.config import settings

from domain.currencies import CURRENCIES
from core.observability import RequestIdFilter, RequestIdMiddleware, build_cors_origins, scrub_sentry_event


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Напоминания о записи: фоновая проверка внутри сервиса (включается REMINDERS_IN_APP=1)
    reminder_task = None
    if settings.reminders_in_app:
        reminder_task = asyncio.create_task(
            reminder_loop(AsyncSessionLocal, settings.reminder_check_interval_seconds),
        )
    yield
    # Shutdown
    if reminder_task:
        reminder_task.cancel()
        with suppress(asyncio.CancelledError):
            await reminder_task
    await engine.dispose()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(request_id)s] %(name)s: %(message)s",
)
for _handler in logging.getLogger().handlers:
    _handler.addFilter(RequestIdFilter())  # номер запроса в каждой строке лога
logger = logging.getLogger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.1,      # 10% запросов для замеров скорости, не 100%
        environment=settings.environment,
        send_default_pii=False,      # не отправлять личные данные пользователей
        before_send=scrub_sentry_event,  # секретные токены из адресов в Sentry не отправляем
    )


# Стандартные /docs, /redoc и /openapi.json выключены: ниже они подключаются заново, с паролем в production
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

API_ROUTES = [
    (organizations.router, "/organizations", "organizations"),
    (users.router, "/users", "users"),
    (clients.router, "/organizations/{organization_id}/clients", "clients"),
    (services.router, "/organizations/{organization_id}/services", "services"),
    (masters.router, "/organizations/{organization_id}/masters", "masters"),
    (appointments.router, "/organizations/{organization_id}/appointments", "appointments"),
    (analytics.router, "/organizations/{organization_id}/analytics", "analytics"),
    (invitations.router, "/invitations", "invitations"),
    (public.router, "/public", "public"),
]

api_v1 = APIRouter(prefix="/api/v1")
legacy_api = APIRouter(prefix="/api")
for _router, _path, _tag in API_ROUTES:
    api_v1.include_router(_router, prefix=_path, tags=[_tag])
    legacy_api.include_router(_router, prefix=_path, tags=[_tag])

# админка платформы только в /api/v1 (старых путей /api/... у неё нет)
api_v1.include_router(admin.router, prefix="/admin", tags=["admin"])
api_v1.include_router(
    notifications.router, prefix="/organizations/{organization_id}/notifications", tags=["notifications"],
)
# ИИ-чат тоже только в /api/v1
api_v1.include_router(ai_chat.router, prefix="/organizations/{organization_id}/ai-chat", tags=["ai-chat"])

app.include_router(api_v1)
# Старые пути /api/... временно работают, но скрыты из документации.
# Удалить, когда фронтенд полностью перейдёт на /api/v1.
app.include_router(legacy_api, include_in_schema=False)



app.add_middleware(
    CORSMiddleware,
    allow_origins=build_cors_origins(settings.cors_origins, settings.frontend_url),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID"],  # чтобы фронтенд мог показать номер запроса в сообщении об ошибке
)
# Добавляется последним, поэтому обрабатывает запрос первым: номер есть даже у ответов CORS
app.add_middleware(RequestIdMiddleware)


def _docs_access(credentials: Annotated[HTTPBasicCredentials | None, Depends(HTTPBasic(auto_error=False))]) -> None:
    """Swagger в production только по логину и паролю из DOCS_USERNAME / DOCS_PASSWORD."""
    if settings.environment != "production":
        return
    if settings.docs_password is None:
        raise HTTPException(status_code=404, detail="Not Found")
    ok = credentials is not None and (
        secrets.compare_digest(credentials.username.encode(), settings.docs_username.encode())
        & secrets.compare_digest(credentials.password.encode(), settings.docs_password.get_secret_value().encode())
    )
    if not ok:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})


@app.get("/openapi.json", include_in_schema=False, dependencies=[Depends(_docs_access)])
async def openapi_schema():
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False, dependencies=[Depends(_docs_access)])
async def swagger_ui():
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{settings.app_name} API")


@app.get("/redoc", include_in_schema=False, dependencies=[Depends(_docs_access)])
async def redoc_ui():
    return get_redoc_html(openapi_url="/openapi.json", title=f"{settings.app_name} API")


@app.get("/health", tags=["health"])
async def health_check(db: Annotated[AsyncSession, Depends(get_db)]):
    try:
        await db.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except Exception:
        logger.exception("Health check: database unavailable")
        raise HTTPException(status_code=503, detail={"status": "error", "database": "unavailable"})


@app.get("/api/v1/currencies", tags=["currencies"])
@app.get("/api/currencies", include_in_schema=False)
async def list_currencies():
    return [{"code": code, **info} for code, info in CURRENCIES.items()]