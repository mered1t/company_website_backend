from fastapi.middleware.cors import CORSMiddleware

from fastapi import FastAPI
from starlette.staticfiles import StaticFiles

from contextlib import asynccontextmanager

from db.database import Base, engine
from routers import users, clients, services, masters, appointments, analytics


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Startup
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    # Shutdown
    await engine.dispose()

app = FastAPI(lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/media", StaticFiles(directory="media"), name="media")

app.include_router(users.router, prefix="/api/users", tags=["users"])
app.include_router(clients.router, prefix="/api/clients", tags=["clients"])
app.include_router(services.router, prefix="/api/services", tags=["services"])
app.include_router(masters.router, prefix="/api/masters", tags=["masters"])
app.include_router(appointments.router, prefix="/api/appointments", tags=["appointments"])
app.include_router(analytics.router, prefix="/api/analytics", tags=["analytics"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)