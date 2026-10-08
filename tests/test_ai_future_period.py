from datetime import UTC, datetime, timedelta

import pytest

from services import ai_service
import models


@pytest.fixture
def fake_model(monkeypatch):
    calls = []

    async def fake(data, language):
        calls.append(data)
        return "Fake report", 1, 1

    monkeypatch.setattr(ai_service, "call_model", fake)
    return calls


def _today():
    return datetime.now(UTC).replace(tzinfo=None).replace(hour=0, minute=0, second=0, microsecond=0)


async def _seed_recent(db, org):
    row = await db.get(models.Organization, org.org_id)
    row.plan = "pro"
    client = models.Client(organization_id=org.org_id, full_name="Anna", phone="+10000001")
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master")
    db.add_all([client, service, master])
    await db.flush()
    start = _today() - timedelta(days=5) + timedelta(hours=10)
    db.add(models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(hours=1), status="completed", price=1000, currency="EUR",
    ))
    await db.commit()


async def test_future_end_date_is_cut_at_today(api, db, org_a, fake_model):
    await _seed_recent(db, org_a)
    today = _today().date()
    body = {
        "date_from": (today - timedelta(days=14)).isoformat(),
        "date_to": (today + timedelta(days=30)).isoformat(),
        "language": "en",
    }
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=body, headers=org_a.headers)
    assert r.status_code == 200
    period = fake_model[0]["period"]
    assert period["to"] == today.isoformat()
    assert "note" in period
    assert r.json()["date_to"] == body["date_to"]  # в ответе то, что просил пользователь


async def test_past_period_has_no_note(api, db, org_a, fake_model):
    await _seed_recent(db, org_a)
    today = _today().date()
    body = {
        "date_from": (today - timedelta(days=14)).isoformat(),
        "date_to": today.isoformat(),
        "language": "en",
    }
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=body, headers=org_a.headers)
    assert r.status_code == 200
    assert "note" not in fake_model[0]["period"]


async def test_fully_future_period_is_rejected_without_calling_model(api, db, org_a, fake_model):
    await _seed_recent(db, org_a)
    today = _today().date()
    body = {
        "date_from": (today + timedelta(days=5)).isoformat(),
        "date_to": (today + timedelta(days=10)).isoformat(),
        "language": "en",
    }
    r = await api.post(f"{org_a.base}/analytics/ai-report", json=body, headers=org_a.headers)
    assert r.status_code == 422
    assert fake_model == []
    usage = await api.get(f"{org_a.base}/analytics/ai-usage", headers=org_a.headers)
    assert usage.json()["used_this_month"] == 0
