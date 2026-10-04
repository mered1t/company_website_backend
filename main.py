from fastapi.middleware.cors import CORSMiddleware
from fastapi import APIRouter, FastAPI, Depends, HTTPException

from contextlib import asynccontextmanager

from db.database import engine, get_db
from routers import users, clients, services, masters, appointments, analytics, organizations, invitations, public

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from rate_limiter import limiter

from sqlalchemy import text
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

import sentry_sdk
import logging
from config import settings

from currencies import CURRENCIES


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Shutdown
    await engine.dispose()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.1,      # 10% запросов для замеров скорости, не 100%
        environment=settings.environment,
        send_default_pii=False,      # не отправлять личные данные пользователей
    )


app = FastAPI(lifespan=lifespan)

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

app.include_router(api_v1)
# Старые пути /api/... временно работают, но скрыты из документации.
# Удалить, когда фронтенд полностью перейдёт на /api/v1.
app.include_router(legacy_api, include_in_schema=False)



app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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