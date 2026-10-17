"""«Любой мастер» на публичной странице: общее свободное время и автоназначение самого свободного мастера."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from core.rate_limiter import limiter
from services.common import get_org_now
from tests.test_booking_manage import SLUG_BASE


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


@pytest.fixture
async def shop(db, org_a):
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    other = models.Service(organization_id=org_a.org_id, name="Nails", price=800, duration_minutes=60)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")    # делает стрижку, утро
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")    # без списка услуг = делает всё, день
    cleo = models.Master(organization_id=org_a.org_id, full_name="Cleo")  # только ногти, рано утром
    db.add_all([cut, other, ann, bob, cleo])
    await db.flush()
    db.add_all([models.MasterService(master_id=ann.id, service_id=cut.id),
                models.MasterService(master_id=cleo.id, service_id=other.id)])
    for master, start, end in ((ann, "09:00", "12:00"), (bob, "13:00", "16:00"), (cleo, "06:00", "08:00")):
        db.add_all([models.WorkingHours(master_id=master.id, day_of_week=d, start_time=start, end_time=end)
                    for d in range(7)])
    await db.commit()
    day = (await get_org_now(db, org_a.org_id)).date() + timedelta(days=2)
    return SimpleNamespace(cut=cut.id, other=other.id, ann=ann.id, bob=bob.id, cleo=cleo.id, day=day)


def _at(day, hour, minute=0):
    return datetime.combine(day, datetime.min.time()).replace(hour=hour, minute=minute)


async def _slots(api, org, shop, **extra):
    r = await api.get(f"{SLUG_BASE}/{org.slug}/available-slots",
                      params={"service_id": shop.cut, "date": shop.day.isoformat(), **extra})
    assert r.status_code == 200, r.text
    return [s["start_time"][11:16] for s in r.json()]


async def _book(api, org, shop, start, phone="+380631110000", **extra):
    return await api.post(f"{SLUG_BASE}/{org.slug}/book", json={
        "client_full_name": "Any Guest", "client_phone": phone, "service_id": shop.cut,
        "start_time": start.isoformat(), **extra})


# ------------------------------------------------------------------ свободное время
async def test_any_master_slots_merge_everyone_who_does_the_service(api, db, org_a, shop):
    merged = await _slots(api, org_a, shop)
    assert merged[0] == "09:00" and "11:00" in merged and "13:00" in merged and "15:00" in merged
    assert "06:00" not in merged and "07:00" not in merged  # Cleo стрижку не делает
    assert merged == sorted(set(merged))
    assert await _slots(api, org_a, shop, master_id=shop.ann) == ["09:00", "09:15", "09:30", "09:45", "10:00",
                                                                    "10:15", "10:30", "10:45", "11:00"]


async def test_any_master_dates_need_at_least_one_free_master(api, db, org_a, shop):
    off = datetime.combine(shop.day, datetime.min.time())
    db.add(models.TimeOff(master_id=shop.ann, start_date=off, end_date=off))  # Ann в отгуле, Bob работает
    both_off = off + timedelta(days=1)
    db.add_all([models.TimeOff(master_id=m, start_date=both_off, end_date=both_off) for m in (shop.ann, shop.bob)])
    await db.commit()
    r = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-dates", params={
        "service_id": shop.cut, "from": shop.day.isoformat(), "to": (shop.day + timedelta(days=1)).isoformat()})
    assert r.status_code == 200, r.text
    assert r.json() == [shop.day.isoformat()]


# ------------------------------------------------------------------ запись без мастера
async def test_booking_without_master_picks_the_one_who_is_free(api, db, org_a, shop):
    r = await _book(api, org_a, shop, _at(shop.day, 10))
    assert r.status_code == 201, r.text
    assert r.json()["master_id"] == shop.ann  # в 10:00 работает только Ann
    r = await _book(api, org_a, shop, _at(shop.day, 14), phone="+380631110001")
    assert r.status_code == 201 and r.json()["master_id"] == shop.bob


async def test_booking_without_master_picks_the_least_busy(api, db, org_a, shop):
    # оба работают с 09 до 19; у Ann в этот день уже две записи, у Bob ни одной
    for master in (shop.ann, shop.bob):
        await db.execute(models.WorkingHours.__table__.delete().where(models.WorkingHours.master_id == master))
        db.add_all([models.WorkingHours(master_id=master, day_of_week=d, start_time="09:00", end_time="19:00")
                    for d in range(7)])
    client = models.Client(organization_id=org_a.org_id, full_name="Regular", phone="+380671110099")
    db.add(client)
    await db.flush()
    for hour in (9, 11):
        start = _at(shop.day, hour)
        db.add(models.Appointment(organization_id=org_a.org_id, client_id=client.id, service_id=shop.cut,
                                  master_id=shop.ann, start_time=start, end_time=start + timedelta(hours=1),
                                  status="scheduled", price=1000, currency="EUR"))
    await db.commit()

    r = await _book(api, org_a, shop, _at(shop.day, 15))
    assert r.status_code == 201, r.text
    assert r.json()["master_id"] == shop.bob


async def test_tie_goes_to_the_first_master(api, db, org_a, shop):
    for master in (shop.ann, shop.bob):
        await db.execute(models.WorkingHours.__table__.delete().where(models.WorkingHours.master_id == master))
        db.add_all([models.WorkingHours(master_id=master, day_of_week=d, start_time="09:00", end_time="19:00")
                    for d in range(7)])
    await db.commit()
    r = await _book(api, org_a, shop, _at(shop.day, 15))
    assert r.status_code == 201 and r.json()["master_id"] == min(shop.ann, shop.bob)


async def test_no_free_master_is_400(api, db, org_a, shop):
    r = await _book(api, org_a, shop, _at(shop.day, 12, 30))  # обед у Ann закончился, Bob ещё не пришёл
    assert r.status_code == 400
    assert r.json()["detail"] == "No master is available at this time. Please choose another time"


async def test_deleted_master_is_never_picked(api, db, org_a, shop):
    from core.time_utils import utc_now
    (await db.get(models.Master, shop.ann)).deleted_at = utc_now()
    await db.commit()
    assert "10:00" not in await _slots(api, org_a, shop)
    assert (await _book(api, org_a, shop, _at(shop.day, 10))).status_code == 400


async def test_unknown_service_is_404_and_explicit_master_still_works(api, db, org_a, shop):
    r = await api.get(f"{SLUG_BASE}/{org_a.slug}/available-slots",
                      params={"service_id": 999999, "date": shop.day.isoformat()})
    assert r.status_code == 404
    r = await _book(api, org_a, shop, _at(shop.day, 14), master_id=shop.bob)
    assert r.status_code == 201 and r.json()["master_id"] == shop.bob
