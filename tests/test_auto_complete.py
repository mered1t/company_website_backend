"""Автоотметка «Пришёл»: прошедшие записи со статусом scheduled становятся completed."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import models
from core.time_utils import utc_now
from services.auto_complete import auto_complete_appointments
from services.common import get_org_now


@pytest.fixture
async def salon(db, org_a):
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=30)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    client = models.Client(organization_id=org_a.org_id, full_name="Olena", phone="+380670001001")
    db.add_all([cut, ann, client])
    await db.commit()
    now = await get_org_now(db, org_a.org_id)
    return SimpleNamespace(cut=cut.id, ann=ann.id, client=client.id, now=now,
                           today=datetime.combine(now.date(), datetime.min.time()))


async def _add(db, org, salon, start, status="scheduled", minutes=30):
    row = models.Appointment(organization_id=org.org_id, client_id=salon.client, service_id=salon.cut,
                             master_id=salon.ann, start_time=start, end_time=start + timedelta(minutes=minutes),
                             status=status, price=1000, currency="EUR")
    db.add(row)
    await db.commit()
    return row.id


async def _state(db, appointment_id):
    db.expire_all()
    row = (await db.execute(select(models.Appointment).where(models.Appointment.id == appointment_id))).scalar_one()
    return row.status, row.auto_completed


async def test_end_of_day_is_the_default(api, db, org_a, salon):
    org = (await api.get("/api/v1/organizations", headers=org_a.headers)).json()[0]
    assert org["auto_complete"] == "end_of_day"
    yesterday = await _add(db, org_a, salon, salon.today - timedelta(days=1) + timedelta(hours=10))
    earlier_today = await _add(db, org_a, salon, salon.today, minutes=1)  # закончилась, но день ещё идёт
    too_old = await _add(db, org_a, salon, salon.today - timedelta(days=10) + timedelta(hours=10))
    no_show = await _add(db, org_a, salon, salon.today - timedelta(days=1) + timedelta(hours=12), status="no_show")

    assert await auto_complete_appointments(db) == 1
    assert await _state(db, yesterday) == ("completed", True)
    assert await _state(db, earlier_today) == ("scheduled", False)
    assert await _state(db, too_old) == ("scheduled", False)   # старше 3 дней — разбирает салон
    assert await _state(db, no_show) == ("no_show", False)
    assert await auto_complete_appointments(db) == 0

    r = await api.get(f"{org_a.base}/appointments/{yesterday}", headers=org_a.headers)
    assert r.json()["auto_completed"] is True


async def test_after_2h_and_off(api, db, org_a, salon):
    url = org_a.base
    assert (await api.patch(url, headers=org_a.headers, json={"auto_complete": "after_2h"})).status_code == 200
    ended_3h_ago = salon.now.replace(second=0, microsecond=0) - timedelta(hours=3, minutes=30)
    ended_1h_ago = salon.now.replace(second=0, microsecond=0) - timedelta(hours=1, minutes=30)
    old = await _add(db, org_a, salon, ended_3h_ago)
    recent = await _add(db, org_a, salon, ended_1h_ago)
    await auto_complete_appointments(db)
    assert await _state(db, old) == ("completed", True)
    assert await _state(db, recent) == ("scheduled", False)

    assert (await api.patch(url, headers=org_a.headers, json={"auto_complete": "off"})).status_code == 200
    yesterday = await _add(db, org_a, salon, salon.today - timedelta(days=1) + timedelta(hours=15))
    await auto_complete_appointments(db)
    assert await _state(db, yesterday) == ("scheduled", False)

    assert (await api.patch(url, headers=org_a.headers, json={"auto_complete": "sometimes"})).status_code == 422
    assert (await api.patch(url, headers=org_a.headers, json={"auto_complete": None})).status_code == 422


async def test_manual_correction_clears_the_flag(api, db, org_a, salon, sent_emails):
    yesterday = await _add(db, org_a, salon, salon.today - timedelta(days=1) + timedelta(hours=10))
    await auto_complete_appointments(db)
    r = await api.patch(f"{org_a.base}/appointments/{yesterday}", headers=org_a.headers, json={"status": "no_show"})
    assert r.status_code == 200 and r.json()["auto_completed"] is False and r.json()["status"] == "no_show"
    assert sent_emails == []


async def test_closed_organization_is_skipped(db, org_a, salon):
    org = await db.get(models.Organization, org_a.org_id)
    org.paid_until = utc_now() - timedelta(days=60)
    org.trial_ends_at = utc_now() - timedelta(days=70)
    await db.commit()
    yesterday = await _add(db, org_a, salon, salon.today - timedelta(days=1) + timedelta(hours=10))
    await auto_complete_appointments(db)
    assert await _state(db, yesterday) == ("scheduled", False)
