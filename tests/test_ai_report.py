import json
from datetime import datetime

import pytest

import ai_service
import models
from config import settings

BODY = {"date_from": "2030-01-07", "date_to": "2030-01-20", "language": "en"}


@pytest.fixture
def fake_model(monkeypatch):
    calls = []

    async def fake(data, language):
        calls.append((data, language))
        return "Fake report", 100, 50

    monkeypatch.setattr(ai_service, "call_model", fake)
    return calls


async def _make_pro(db, org):
    row = await db.get(models.Organization, org.org_id)
    row.plan = "pro"
    await db.commit()


async def _seed(db, org):
    client_a = models.Client(organization_id=org.org_id, full_name="Anna Secret", phone="+10000001")
    client_b = models.Client(organization_id=org.org_id, full_name="Boris Secret", phone="+10000002")
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master One")
    db.add_all([client_a, client_b, service, master])
    await db.flush()
    for client, day, hour, price in [(client_a, 7, 10, 1000), (client_a, 14, 10, 1000), (client_b, 14, 11, 2000)]:
        db.add(models.Appointment(
            organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
            start_time=datetime(2030, 1, day, hour), end_time=datetime(2030, 1, day, hour + 1),
            status="completed", price=price, currency="EUR",
        ))
    await db.commit()


async def test_basic_plan_cannot_use_ai(api, db, org_a, fake_model):
    await _seed(db, org_a)
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=BODY, headers=org_a.headers)
    assert r.status_code == 403
    assert fake_model == []


async def test_usage_shows_plan_and_limit(api, db, org_a):
    r = await api.get(f"{org_a.base}/analytics/ai-usage", headers=org_a.headers)
    assert r.status_code == 200
    assert r.json() == {"plan": "basic", "ai_enabled": False, "used_this_month": 0, "monthly_limit": settings.ai_monthly_limit}


async def test_no_data_does_not_call_model(api, db, org_a, fake_model):
    await _make_pro(db, org_a)
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=BODY, headers=org_a.headers)
    assert r.status_code == 422
    assert fake_model == []


async def test_report_is_generated_then_cached(api, db, org_a, fake_model):
    await _make_pro(db, org_a)
    await _seed(db, org_a)
    url = f"{org_a.base}/analytics/ai-report"

    r = await api.post(url, json=BODY, headers=org_a.headers)
    assert r.status_code == 200
    data = r.json()
    assert data["content"] == "Fake report"
    assert data["cached"] is False
    assert data["used_this_month"] == 1

    r = await api.post(url, json=BODY, headers=org_a.headers)
    assert r.status_code == 200
    assert r.json()["cached"] is True
    assert r.json()["used_this_month"] == 1
    assert len(fake_model) == 1  # второй раз модель не вызывали


async def test_other_language_is_a_new_report(api, db, org_a, fake_model):
    await _make_pro(db, org_a)
    await _seed(db, org_a)
    url = f"{org_a.base}/analytics/ai-report"
    await api.post(url, json=BODY, headers=org_a.headers)
    r = await api.post(url, json={**BODY, "language": "pl"}, headers=org_a.headers)
    assert r.status_code == 200
    assert r.json()["cached"] is False
    assert fake_model[1][1] == "pl"
    usage = await api.get(f"{org_a.base}/analytics/ai-usage", headers=org_a.headers)
    assert usage.json()["used_this_month"] == 2


async def test_monthly_limit(api, db, org_a, fake_model, monkeypatch):
    monkeypatch.setattr(settings, "ai_monthly_limit", 1)
    await _make_pro(db, org_a)
    await _seed(db, org_a)
    url = f"{org_a.base}/analytics/ai-report"
    assert (await api.post(url, json=BODY, headers=org_a.headers)).status_code == 200
    r = await api.post(url, json={**BODY, "language": "es"}, headers=org_a.headers)
    assert r.status_code == 429
    assert len(fake_model) == 1


async def test_model_failure_returns_503_and_is_not_counted(api, db, org_a, monkeypatch):
    async def broken(data, language):
        raise RuntimeError("OpenAI is down")

    monkeypatch.setattr(ai_service, "call_model", broken)
    await _make_pro(db, org_a)
    await _seed(db, org_a)
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=BODY, headers=org_a.headers)
    assert r.status_code == 503
    usage = await api.get(f"{org_a.base}/analytics/ai-usage", headers=org_a.headers)
    assert usage.json()["used_this_month"] == 0


async def test_not_configured_model_returns_503(api, db, org_a, monkeypatch):
    monkeypatch.setattr(settings, "openai_api_key", None)
    await _make_pro(db, org_a)
    await _seed(db, org_a)
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=BODY, headers=org_a.headers)
    assert r.status_code == 503
    usage = await api.get(f"{org_a.base}/analytics/ai-usage", headers=org_a.headers)
    assert usage.json()["used_this_month"] == 0


async def test_client_names_and_phones_are_not_sent_to_model(api, db, org_a, fake_model):
    await _make_pro(db, org_a)
    await _seed(db, org_a)
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=BODY, headers=org_a.headers)
    assert r.status_code == 200
    payload = json.dumps(fake_model[0][0], ensure_ascii=False)
    for secret in ("Anna", "Boris", "+1000000"):
        assert secret not in payload
    assert fake_model[0][0]["currency"] == "EUR"
    assert fake_model[0][0]["revenue"]["total"] == 40.0  # 4000 минимальных единиц = 40.00 EUR


async def test_period_validation(api, db, org_a, fake_model):
    await _make_pro(db, org_a)
    url = f"{org_a.base}/analytics/ai-report"
    r = await api.post(url, json={"date_from": "2030-01-20", "date_to": "2030-01-10"}, headers=org_a.headers)
    assert r.status_code == 422
    r = await api.post(url, json={"date_from": "2020-01-01", "date_to": "2030-01-01"}, headers=org_a.headers)
    assert r.status_code == 422
    r = await api.post(url, json={**BODY, "language": "xx"}, headers=org_a.headers)
    assert r.status_code == 422
    assert fake_model == []
