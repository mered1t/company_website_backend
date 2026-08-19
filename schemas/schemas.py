from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class UserBase(BaseModel):
    username: str = Field(min_length=1, max_length=50)
    email: EmailStr = Field(max_length=120)


class UserCreate(UserBase):
    password: str = Field(min_length=8)


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    image_file: str | None
    image_path: str


class UserPrivate(UserPublic):
    email: EmailStr


class UserUpdate(BaseModel):
    username: str | None = Field(default=None, min_length=1, max_length=50)
    email: EmailStr | None = Field(default=None, max_length=120)
    image_file: str | None = Field(default=None, min_length=1, max_length=200)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"

class ClientBase(BaseModel):
    full_name: str = Field(min_length=1, max_length=150)
    phone: str = Field(min_length=5, max_length=20)
    email: EmailStr | None = None
    birth_date: date | None = None
    notes: str | None = None


class ClientCreate(ClientBase):
    pass


class ClientUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    phone: str | None = Field(default=None, min_length=5, max_length=20)
    email: EmailStr | None = None
    birth_date: date | None = None
    notes: str | None = None


class ClientPublic(ClientBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class ServiceBase(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    price: int = Field(ge=0)
    duration_minutes: int = Field(gt=0)
    description: str | None = None
    photo: str | None = None


class ServiceCreate(ServiceBase):
    pass


class ServiceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    price: int | None = Field(default=None, ge=0)
    duration_minutes: int | None = Field(default=None, gt=0)
    description: str | None = None
    photo: str | None = None


class ServicePublic(ServiceBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime


class WorkingHoursBase(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")


class WorkingHoursPublic(WorkingHoursBase):
    model_config = ConfigDict(from_attributes=True)

    id: int


class MasterBase(BaseModel):
    full_name: str = Field(min_length=1, max_length=150)
    phone: str | None = None
    photo: str | None = None


class MasterCreate(MasterBase):
    working_hours: list[WorkingHoursBase] = []


class MasterUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=150)
    phone: str | None = None
    photo: str | None = None


class MasterPublic(MasterBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    created_at: datetime
    working_hours: list[WorkingHoursPublic] = []