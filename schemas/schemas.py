from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator


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



class WorkingHoursFields(BaseModel):
    day_of_week: int = Field(ge=0, le=6)
    start_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")
    end_time: str = Field(pattern=r"^([01]\d|2[0-3]):([0-5]\d)$")


class WorkingHoursBase(WorkingHoursFields):
    @model_validator(mode="after")
    def check_time_order(self) -> "WorkingHoursBase":
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be earlier than end_time")
        return self


class WorkingHoursPublic(WorkingHoursFields):
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



class AppointmentBase(BaseModel):
    client_id: int
    service_id: int
    master_id: int
    start_time: datetime
    notes: str | None = None


class AppointmentCreate(AppointmentBase):
    pass


class AppointmentUpdate(BaseModel):
    client_id: int | None = None
    service_id: int | None = None
    master_id: int | None = None
    start_time: datetime | None = None
    status: str | None = None
    notes: str | None = None


class AppointmentPublic(AppointmentBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    end_time: datetime
    status: str
    created_at: datetime

class AppointmentWithDetails(AppointmentPublic):
    model_config = ConfigDict(from_attributes=True)

    client_name: str
    service_name: str
    service_price: int
    master_name: str