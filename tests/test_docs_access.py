"""Swagger в production закрыт логином и паролем; локально и в тестах открыт."""
import base64

import pytest
from pydantic import SecretStr

from core.config import settings

PAGES = ["/docs", "/redoc", "/openapi.json"]


def _basic(user, password):
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
def production(monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "docs_username", "owner")
    monkeypatch.setattr(settings, "docs_password", SecretStr("S3cret-docs"))


async def test_docs_are_open_outside_production(api):
    for page in PAGES:
        assert (await api.get(page)).status_code == 200, page
    assert any(p.startswith("/api/v1/") for p in (await api.get("/openapi.json")).json()["paths"])


async def test_production_without_a_password_has_no_docs(api, monkeypatch):
    monkeypatch.setattr(settings, "environment", "production")
    monkeypatch.setattr(settings, "docs_password", None)
    for page in PAGES:
        assert (await api.get(page)).status_code == 404, page


@pytest.mark.parametrize("headers", [{}, _basic("owner", "wrong"), _basic("someone", "S3cret-docs")])
async def test_production_docs_need_the_right_login(api, production, headers):
    for page in PAGES:
        r = await api.get(page, headers=headers)
        assert r.status_code == 401, page
        assert r.headers["www-authenticate"] == "Basic"


async def test_production_docs_open_with_the_right_login(api, production):
    for page in PAGES:
        assert (await api.get(page, headers=_basic("owner", "S3cret-docs"))).status_code == 200, page
    schema = (await api.get("/openapi.json", headers=_basic("owner", "S3cret-docs"))).json()
    assert "/api/v1/admin/organizations" in schema["paths"]


async def test_the_api_itself_does_not_need_the_docs_password(api, production):
    assert (await api.get("/health")).status_code == 200
    assert (await api.get("/api/v1/currencies")).status_code == 200
