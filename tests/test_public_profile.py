"""Данные для публичной страницы записи: контакты и описание салона, часы работы, срок отмены, должность мастера."""
import pytest

import models
from core.config import settings
from core.time_utils import utc_now
from tests.test_booking_manage import SLUG_BASE
from tests.test_org_data import _add_member

PROFILE = {
    "address": "Kyiv, Khreshchatyk 1",
    "phone": "+380441234567",
    "logo_url": "https://cdn.example.com/logo.png",
    "description": "Barbershop in the city centre",
    "privacy_policy_url": "https://example.com/privacy",
}


async def test_salon_fills_its_profile_and_the_public_page_shows_it(api, db, org_a):
    r = await api.patch(org_a.base, headers=org_a.headers, json=PROFILE)
    assert r.status_code == 200, r.text
    assert {k: r.json()[k] for k in PROFILE} == PROFILE

    [listed] = (await api.get("/api/v1/organizations", headers=org_a.headers)).json()
    assert {k: listed[k] for k in PROFILE} == PROFILE

    public = (await api.get(f"{SLUG_BASE}/{org_a.slug}")).json()
    assert {k: public[k] for k in PROFILE} == PROFILE
    assert public["cancellation_hours"] == settings.booking_change_cutoff_hours


async def test_profile_fields_can_be_cleared(api, db, org_a):
    await api.patch(org_a.base, headers=org_a.headers, json=PROFILE)
    r = await api.patch(org_a.base, headers=org_a.headers, json={k: None for k in PROFILE})
    assert r.status_code == 200, r.text
    public = (await api.get(f"{SLUG_BASE}/{org_a.slug}")).json()
    assert all(public[k] is None for k in PROFILE)


@pytest.mark.parametrize("body", [
    {"phone": "044 123 45 67"},
    {"logo_url": "javascript:alert(1)"},
    {"privacy_policy_url": "ftp://example.com/x"},
    {"logo_url": "https://example.com/a b.png"},
    {"description": "x" * 1001},
    {"address": "x" * 301},
])
async def test_bad_profile_values_are_422(api, db, org_a, body):
    assert (await api.patch(org_a.base, headers=org_a.headers, json=body)).status_code == 422


async def test_master_cannot_edit_the_salon_profile(api, db, org_a):
    headers, _ = await _add_member(db, org_a, "master")
    assert (await api.patch(org_a.base, headers=headers, json={"address": "x"})).status_code == 403


async def test_opening_hours_come_from_the_masters_schedules(api, db, org_a):
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    bob = models.Master(organization_id=org_a.org_id, full_name="Bob")
    gone = models.Master(organization_id=org_a.org_id, full_name="Gone", deleted_at=utc_now())
    db.add_all([ann, bob, gone])
    await db.flush()
    db.add_all([
        models.WorkingHours(master_id=ann.id, day_of_week=0, start_time="09:00", end_time="13:00"),
        models.WorkingHours(master_id=ann.id, day_of_week=0, start_time="14:00", end_time="18:00"),
        models.WorkingHours(master_id=bob.id, day_of_week=0, start_time="10:00", end_time="20:00"),
        models.WorkingHours(master_id=bob.id, day_of_week=5, start_time="11:00", end_time="16:00"),
        models.WorkingHours(master_id=gone.id, day_of_week=6, start_time="07:00", end_time="23:00"),
    ])
    await db.commit()
    public = (await api.get(f"{SLUG_BASE}/{org_a.slug}")).json()
    assert public["opening_hours"] == [
        {"day_of_week": 0, "start_time": "09:00", "end_time": "20:00"},
        {"day_of_week": 5, "start_time": "11:00", "end_time": "16:00"},
    ]


async def test_master_title_and_bio(api, db, org_a):
    r = await api.post(f"{org_a.base}/masters", headers=org_a.headers, json={
        "full_name": "Taras", "title": "Barber", "bio": "10 years with clippers"})
    assert r.status_code == 201, r.text
    master_id = r.json()["id"]
    assert (r.json()["title"], r.json()["bio"]) == ("Barber", "10 years with clippers")

    r = await api.patch(f"{org_a.base}/masters/{master_id}", headers=org_a.headers, json={"title": "Senior barber"})
    assert r.status_code == 200 and r.json()["title"] == "Senior barber"

    [public] = [m for m in (await api.get(f"{SLUG_BASE}/{org_a.slug}/masters")).json() if m["id"] == master_id]
    assert (public["title"], public["bio"]) == ("Senior barber", "10 years with clippers")
    assert "phone" not in public

    too_long = await api.patch(f"{org_a.base}/masters/{master_id}", headers=org_a.headers, json={"bio": "x" * 501})
    assert too_long.status_code == 422


async def test_export_includes_the_new_fields(api, db, org_a):
    await api.patch(org_a.base, headers=org_a.headers, json=PROFILE)
    await api.post(f"{org_a.base}/masters", headers=org_a.headers, json={"full_name": "Taras", "title": "Barber"})
    data = (await api.get(f"{org_a.base}/export", headers=org_a.headers)).json()
    assert data["organization"]["address"] == PROFILE["address"]
    assert data["masters"][0]["title"] == "Barber"
