from typing import Annotated

from fastapi import FastAPI, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.staticfiles import StaticFiles

from db.database import Base, engine, get_db
from models import models
from routers import users

Base.metadata.create_all(bind=engine)

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/media", StaticFiles(directory="media"), name="media")

app.include_router(users.router, prefix="/api/users", tags=["users"])

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:4200",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/", include_in_schema=False, name="home")
def home(db: Annotated[Session, Depends(get_db)]):
    result = db.execute(select(models.User))
    posts = result.scalars().all()
    return posts