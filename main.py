from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, Depends, HTTPException

from contextlib import asynccontextmanager

from db.database import Base, engine, get_db
from routers import users, clients, services, masters, appointments, analytics, organizations, invitations, public

from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from rate_limiter import limiter

from sqlalchemy import text
from typing import Annotated
from sqlalchemy.ext.asyncio import AsyncSession

import sentry_sdk
from config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    # Shutdown
    await engine.dispose()


if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=1.0,
        environment="production",
    )


app = FastAPI(lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.include_router(organizations.router, prefix="/api/organizations", tags=["organizations"])
app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(clients.router, prefix="/api/organizations/{organization_id}/clients", tags=["clients"])
app.include_router(services.router, prefix="/api/organizations/{organization_id}/services", tags=["services"])
app.include_router(masters.router, prefix="/api/organizations/{organization_id}/masters", tags=["masters"])
app.include_router(appointments.router, prefix="/api/organizations/{organization_id}/appointments", tags=["appointments"])
app.include_router(analytics.router, prefix="/api/organizations/{organization_id}/analytics", tags=["analytics"])
app.include_router(invitations.router, prefix="/api/invitations", tags=["invitations"])
app.include_router(public.router, prefix="/api/public", tags=["public"])



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
        raise HTTPException(status_code=503, detail={"status": "error", "database": "unavailable"})