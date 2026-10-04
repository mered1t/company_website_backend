from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from tests.test_tenant_isolation import make_client, make_service

MASTER_BUSY = "Master already has an appointment at this time"


def tomorrow_at(hour: int, minute: int = 0) -> datetime:
    """Завтра в указанное время по часовому поясу организации (в тестах Europe/Kiev)."""
    local_now = datetime.now(ZoneInfo("Europe/Kiev")).replace(tzinfo=None)
    return (local_now + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


async def make_working_master(api, org, service_ids):
    r = await api.post(f"{org.base}/masters", headers=org.headers, json={
        "full_name": "Pro Master", "phone": "+380991112244", "service_ids": service_ids,
        "working_hours": [{"day_of_week": d, "start_time": "09:00", "end_time": "18:00"} for d in range(7)],
    })
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
async def shop(api, org_a):
    service = await make_service(api, org_a, "Haircut")          # 100
    service_b = await make_service(api, org_a, "Coloring")
    r = await api.patch(f"{org_a.base}/services/{service_b}", headers=org_a.headers, json={"price": 250})
    assert r.status_code == 200, r.text
    master = await make_working_master(api, org_a, [service, service_b])
    client = await make_client(api, org_a, "+380990000060")
    return SimpleNamespace(service=service, service_b=service_b, master=master, client=client)


async def admin_book(api, org, shop, start, **overrides):
    body = {"client_id": shop.client, "service_id": shop.service, "master_id": shop.master,
            "start_time": start.isoformat()}
    body.update(overrides)
    return await api.post(f"{org.base}/appointments", headers=org.headers, json=body)


def public_body(shop, start, phone="+380991234567"):
    return {"client_full_name": "Walk In", "client_phone": phone, "service_id": shop.service,
            "master_id": shop.master, "start_time": start.isoformat()}


# ---------------- запись из админки ----------------
async def test_admin_create_appointment_success(api, org_a, shop):
    start = tomorrow_at(12)
    r = await admin_book(api, org_a, shop, start)
    assert r.status_code == 201, r.text
    data = r.json()
    assert datetime.fromisoformat(data["end_time"]) == start + timedelta(minutes=60)
    assert (data["price"], data["currency"], data["status"]) == (100, "EUR", "scheduled")


async def test_admin_overlapping_appointment_rejected(api, org_a, shop):
    assert (await admin_book(api, org_a, shop, tomorrow_at(12))).status_code == 201
    r = await admin_book(api, org_a, shop, tomorrow_at(12, 30))
    assert r.status_code == 400
    assert r.json()["detail"] == MASTER_BUSY


async def test_admin_back_to_back_is_allowed(api, org_a, shop):
    assert (await admin_book(api, org_a, shop, tomorrow_at(12))).status_code == 201
    assert (await admin_book(api, org_a, shop, tomorrow_at(13))).status_code == 201


async def test_admin_outside_working_hours_rejected(api, org_a, shop):
    r = await admin_book(api, org_a, shop, tomorrow_at(7))
    assert r.status_code == 400
    assert r.json()["detail"] == "Appointment time is outside master's working hours"


async def test_admin_service_master_does_not_provide_rejected(api, org_a, shop):
    other = await make_service(api, org_a, "Shaving")
    r = await admin_book(api, org_a, shop, tomorrow_at(12), service_id=other)
    assert r.status_code == 400
    assert r.json()["detail"] == "This master does not provide this service"


async def test_admin_foreign_client_rejected(api, org_a, org_b, shop):
    foreign_client = await make_client(api, org_b, "+380990000061")
    r = await admin_book(api, org_a, shop, tomorrow_at(12), client_id=foreign_client)
    assert r.status_code == 404


async def test_reschedule_to_free_time(api, org_a, shop):
    appt = (await admin_book(api, org_a, shop, tomorrow_at(12))).json()
    r = await api.patch(f"{org_a.base}/appointments/{appt['id']}", headers=org_a.headers,
                        json={"start_time": tomorrow_at(15).isoformat()})
    assert r.status_code == 200, r.text
    assert datetime.fromisoformat(r.json()["end_time"]) == tomorrow_at(16)


async def test_reschedule_onto_busy_time_rejected(api, org_a, shop):
    await admin_book(api, org_a, shop, tomorrow_at(12))
    second = (await admin_book(api, org_a, shop, tomorrow_at(15))).json()
    r = await api.patch(f"{org_a.base}/appointments/{second['id']}", headers=org_a.headers,
                        json={"start_time": tomorrow_at(12, 30).isoformat()})
    assert r.status_code == 400
    assert r.json()["detail"] == MASTER_BUSY


async def test_changing_service_takes_new_price(api, org_a, shop):
    appt = (await admin_book(api, org_a, shop, tomorrow_at(12))).json()
    r = await api.patch(f"{org_a.base}/appointments/{appt['id']}", headers=org_a.headers,
                        json={"service_id": shop.service_b})
    assert r.status_code == 200, r.text
    assert r.json()["price"] == 250


async def test_cancelled_appointment_frees_the_time(api, org_a, shop):
    appt = (await admin_book(api, org_a, shop, tomorrow_at(12))).json()
    r = await api.patch(f"{org_a.base}/appointments/{appt['id']}", headers=org_a.headers,
                        json={"status": "cancelled"})
    assert r.status_code == 200, r.text
    assert (await admin_book(api, org_a, shop, tomorrow_at(12))).status_code == 201


async def test_deleted_appointment_frees_the_time(api, org_a, shop):
    appt = (await admin_book(api, org_a, shop, tomorrow_at(12))).json()
    assert (await api.delete(f"{org_a.base}/appointments/{appt['id']}", headers=org_a.headers)).status_code == 204
    assert (await admin_book(api, org_a, shop, tomorrow_at(12))).status_code == 201


# ---------------- публичная запись ----------------
async def _slots(api, org, shop, day: datetime):
    r = await api.get(f"/api/v1/public/{org.slug}/available-slots",
                      params={"master_id": shop.master, "service_id": shop.service, "date": day.date().isoformat()})
    assert r.status_code == 200, r.text
    return r.json()


async def test_public_slots_for_a_free_day(api, org_a, shop):
    slots = await _slots(api, org_a, shop, tomorrow_at(0))
    assert len(slots) == 33  # 09:00..17:00 с шагом 15 минут, услуга 60 минут


async def test_public_slots_exclude_booked_time(api, org_a, shop):
    assert (await admin_book(api, org_a, shop, tomorrow_at(12))).status_code == 201
    slots = await _slots(api, org_a, shop, tomorrow_at(0))
    assert len(slots) == 26  # исчезли слоты 11:15 ... 12:45


async def test_public_available_dates_include_tomorrow(api, org_a, shop):
    day = tomorrow_at(0)
    r = await api.get(f"/api/v1/public/{org_a.slug}/available-dates",
                      params={"master_id": shop.master, "service_id": shop.service, "month": day.strftime("%Y-%m")})
    assert r.status_code == 200, r.text
    assert day.date().isoformat() in r.json()


async def test_public_booking_success_creates_client(api, org_a, shop):
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=public_body(shop, tomorrow_at(12)))
    assert r.status_code == 201, r.text
    assert (r.json()["price"], r.json()["currency"]) == (100, "EUR")
    clients = (await api.get(f"{org_a.base}/clients", headers=org_a.headers)).json()
    assert "+380991234567" in [c["phone"] for c in clients]


async def test_public_booking_overlap_rejected(api, org_a, shop):
    assert (await api.post(f"/api/v1/public/{org_a.slug}/book", json=public_body(shop, tomorrow_at(12)))).status_code == 201
    r = await api.post(f"/api/v1/public/{org_a.slug}/book",
                       json=public_body(shop, tomorrow_at(12, 30), phone="+380997654321"))
    assert r.status_code == 400
    assert r.json()["detail"] == MASTER_BUSY


async def test_public_booking_in_the_past_rejected(api, org_a, shop):
    yesterday = tomorrow_at(12) - timedelta(days=2)
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=public_body(shop, yesterday))
    assert r.status_code == 400
    assert r.json()["detail"] == "Cannot book a time in the past"


async def test_public_booking_limit_of_three_upcoming(api, org_a, shop):
    for hour in (10, 12, 14):
        r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=public_body(shop, tomorrow_at(hour)))
        assert r.status_code == 201, r.text
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=public_body(shop, tomorrow_at(16)))
    assert r.status_code == 400
    assert r.json()["detail"].startswith("You already have 3 upcoming appointments")


async def test_time_off_blocks_public_booking(api, org_a, shop):
    day = tomorrow_at(0).date().isoformat()
    r = await api.post(f"{org_a.base}/masters/{shop.master}/time-off", headers=org_a.headers,
                       json={"start_date": day, "end_date": day, "reason": "vacation"})
    assert r.status_code == 201, r.text

    assert await _slots(api, org_a, shop, tomorrow_at(0)) == []
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=public_body(shop, tomorrow_at(12)))
    assert r.status_code == 400
    assert r.json()["detail"] == "Master does not work on this day"