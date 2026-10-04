"""Общие настройки тестов. Тесты работают ТОЛЬКО с базой crm_test."""
import asyncio
from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from config import settings

# ---------- ЗАЩИТА: тесты только на локальной базе crm_test ----------
_url = make_url(settings.database_url).set(database="crm_test")
if _url.host not in ("localhost", "127.0.0.1"):
    raise RuntimeError(
        f"Тесты разрешены только на локальной БД, а в .env хост: {_url.host}. Остановлено."
    )
TEST_DATABASE_URL = _url.render_as_string(hide_password=False)
settings.database_url = TEST_DATABASE_URL  # приложение теперь смотрит на crm_test
settings.sentry_dsn = None                 # в Sentry из тестов ничего не шлём
# --------------------------------------------------------------------

import models  # noqa: E402,F401  (регистрирует все таблицы)
from db.database import Base, get_db  # noqa: E402
from main import app  # noqa: E402
from rate_limiter import limiter  # noqa: E402

limiter.enabled = False  # иначе на регистрации/логине словим 429

test_engine = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
TestSession = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


async def _override_get_db():
    async with TestSession() as session:
        yield session


app.dependency_overrides[get_db] = _override_get_db


@pytest.fixture(scope="session", autouse=True)
def prepare_database():
    """Один раз за запуск: пересоздаём все таблицы в crm_test."""
    async def _reset():
        eng = create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)
        async with eng.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        await eng.dispose()

    asyncio.run(_reset())


@pytest.fixture(autouse=True)
async def clean_db():
    """После каждого теста очищаем все таблицы, чтобы тесты не влияли друг на друга."""
    yield
    async with test_engine.begin() as conn:
        tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture(autouse=True)
def no_real_emails(monkeypatch):
    import resend
    monkeypatch.setattr(resend.Emails, "send", staticmethod(lambda *a, **k: {"id": "test"}))


@pytest.fixture
async def api():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def db():
    async with TestSession() as session:
        yield session


@dataclass
class Tenant:
    org_id: int
    slug: str
    headers: dict

    @property
    def base(self) -> str:
        return f"/api/v1/organizations/{self.org_id}"


async def _create_tenant(api, name: str) -> Tenant:
    email = f"{name}@example.com"
    password = "Passw0rd!"

    r = await api.post("/api/v1/users", json={
        "username": name, "email": email, "password": password, "accept_terms": True,
    })
    assert r.status_code in (200, 201), f"register failed: {r.status_code} {r.text}"

    r = await api.post("/api/v1/users/token", data={"username": email, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = await api.post("/api/v1/organizations", json={"name": f"Shop {name}", "timezone": "Europe/Kiev"},
                       headers=headers)
    assert r.status_code in (200, 201), f"create org failed: {r.status_code} {r.text}"
    return Tenant(org_id=r.json()["id"], slug=r.json()["slug"], headers=headers)


@pytest.fixture
async def org_a(api):
    return await _create_tenant(api, "ownera")


@pytest.fixture
async def org_b(api):
    return await _create_tenant(api, "ownerb")

@pytest.fixture
def sent_emails(monkeypatch):
    """Список отправленных писем (payload для resend). Токены берём из html, как пользователь из ссылки."""
    import resend

    sent = []

    def fake_send(payload, *args, **kwargs):
        sent.append(payload)
        return {"id": "test"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(fake_send))
    return sent
