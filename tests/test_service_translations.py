"""Переводы услуг: хранение, показ на публичной странице по lang, черновик через ИИ."""
import pytest

import models
from core.rate_limiter import limiter
from services import translations as tr
from tests.test_booking_manage import SLUG_BASE

PL = {"name": "Strzyżenie", "description": "Mycie i stylizacja"}


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _service(api, org, **extra):
    r = await api.post(f"{org.base}/services", headers=org.headers, json={
        "name": "Стрижка", "description": "Миття та укладка", "price": 1500, "duration_minutes": 45, **extra})
    assert r.status_code == 201, r.text
    return r.json()


async def test_translations_are_saved_and_replaced(api, org_a):
    service = await _service(api, org_a, translations={"pl": PL, "en": {"name": "Haircut"}})
    assert service["translations"]["pl"] == PL and service["translations"]["en"]["description"] is None
    url = f"{org_a.base}/services/{service['id']}"
    r = await api.patch(url, headers=org_a.headers, json={"translations": {"es": {"name": "Corte"}}})
    assert r.status_code == 200 and set(r.json()["translations"]) == {"es"}  # заменяет целиком
    assert (await api.patch(url, headers=org_a.headers, json={"translations": {}})).json()["translations"] == {}
    assert (await api.patch(url, headers=org_a.headers, json={"translations": None})).status_code == 422
    bad = [{"de": {"name": "Haarschnitt"}}, {"pl": {"name": ""}}, {"pl": {"name": "x" * 151}},
           {"pl": {"description": "no name"}}]
    for value in bad:
        assert (await api.patch(url, headers=org_a.headers, json={"translations": value})).status_code == 422, value


async def test_public_page_uses_the_language(api, db, org_a):
    service = await _service(api, org_a, translations={"pl": PL, "en": {"name": "Haircut"}})
    master = models.Master(organization_id=org_a.org_id, full_name="Ann")
    db.add(master)
    await db.flush()
    db.add(models.MasterService(master_id=master.id, service_id=service["id"]))
    await db.commit()

    def first(r):
        return r.json()[0]["name"], r.json()[0]["description"]

    assert first(await api.get(f"{SLUG_BASE}/{org_a.slug}/services", params={"lang": "pl"})) == (PL["name"], PL["description"])
    # без описания в переводе — исходное описание
    assert first(await api.get(f"{SLUG_BASE}/{org_a.slug}/services", params={"lang": "en"})) == ("Haircut", "Миття та укладка")
    assert first(await api.get(f"{SLUG_BASE}/{org_a.slug}/services", params={"lang": "sq"}))[0] == "Стрижка"
    assert first(await api.get(f"{SLUG_BASE}/{org_a.slug}/services"))[0] == "Стрижка"
    assert (await api.get(f"{SLUG_BASE}/{org_a.slug}/services", params={"lang": "xx"})).status_code == 422
    masters = await api.get(f"{SLUG_BASE}/{org_a.slug}/masters", params={"lang": "pl"})
    assert masters.json()[0]["services"][0]["name"] == PL["name"]


async def test_ai_draft(api, org_a, monkeypatch):
    seen = []

    async def fake(payload):
        seen.append(payload)
        return {"pl": PL, "en": {"name": "Haircut", "description": ""}, "es": {"name": ""}, "de": {"name": "X"}}

    monkeypatch.setattr(tr, "call_translation_model", fake)
    url = f"{org_a.base}/services/translate"
    r = await api.post(url, headers=org_a.headers, json={"name": "Стрижка", "description": "Миття",
                                                         "source_language": "uk"})
    assert r.status_code == 200, r.text
    assert r.json()["translations"] == {"pl": PL, "en": {"name": "Haircut", "description": None}}  # пустое отброшено
    assert set(seen[0]["targets"]) == {"en", "pl", "es", "sq"}  # без исходного языка
    r = await api.post(url, headers=org_a.headers, json={"name": "Стрижка", "target_languages": ["pl"]})
    assert set(r.json()["translations"]) == {"pl"}


async def test_ai_draft_errors(api, org_a, monkeypatch):
    async def broken(payload):
        raise ValueError("not json")

    monkeypatch.setattr(tr, "call_translation_model", broken)
    r = await api.post(f"{org_a.base}/services/translate", headers=org_a.headers, json={"name": "Стрижка"})
    assert r.status_code == 502
    monkeypatch.undo()
    from core.config import settings
    monkeypatch.setattr(settings, "openai_api_key", None)
    r = await api.post(f"{org_a.base}/services/translate", headers=org_a.headers, json={"name": "Стрижка"})
    assert r.status_code == 503
