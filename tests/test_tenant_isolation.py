"""Проверка изоляции организаций: одна организация не должна трогать данные другой."""
from datetime import datetime
from types import SimpleNamespace

import pytest

import models


# ---------- помощники для подготовки данных ----------
async def make_service(api, t, name="Haircut"):
    r = await api.post(f"{t.base}/services", headers=t.headers,
                       json={"name": name, "duration_minutes": 60, "price": 100})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_client(api, t, phone):
    r = await api.post(f"{t.base}/clients", headers=t.headers,
                       json={"full_name": "Test Client", "phone": phone})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_master(api, t, service_ids=None):
    r = await api.post(f"{t.base}/masters", headers=t.headers,
                       json={"full_name": "Test Master", "phone": "+380991112233",
                             "working_hours": [], "service_ids": service_ids or []})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def make_appointment(db, t, client_id, service_id, master_id):
    # напрямую в БД, чтобы не зависеть от рабочих часов мастера
    appt = models.Appointment(
        organization_id=t.org_id, client_id=client_id, service_id=service_id,
        master_id=master_id, start_time=datetime(2030, 1, 7, 10, 0),
        end_time=datetime(2030, 1, 7, 11, 0),
    )
    db.add(appt)
    await db.commit()
    await db.refresh(appt)
    return appt.id


@pytest.fixture
async def shop_a(api, db, org_a):
    service_id = await make_service(api, org_a)
    client_id = await make_client(api, org_a, "+380990000001")
    client2_id = await make_client(api, org_a, "+380990000002")
    master_id = await make_master(api, org_a)
    appt_id = await make_appointment(db, org_a, client_id, service_id, master_id)
    return SimpleNamespace(client2_id=client2_id, appt_id=appt_id)


# ---------- C1: запись нельзя перепривязать к чужим данным ----------
async def test_control_patch_appointment_with_own_client_ok(api, org_a, shop_a):
    """Контроль: со СВОИМ клиентом всё работает (значит, URL и данные в тесте верные)."""
    r = await api.patch(f"{org_a.base}/appointments/{shop_a.appt_id}", headers=org_a.headers,
                        json={"client_id": shop_a.client2_id})
    assert r.status_code == 200, r.text


async def test_patch_appointment_foreign_client_404(api, org_a, org_b, shop_a):
    foreign_client = await make_client(api, org_b, "+380990000003")
    r = await api.patch(f"{org_a.base}/appointments/{shop_a.appt_id}", headers=org_a.headers,
                        json={"client_id": foreign_client})
    assert r.status_code == 404, r.text


async def test_patch_appointment_foreign_master_404(api, org_a, org_b, shop_a):
    foreign_master = await make_master(api, org_b)
    r = await api.patch(f"{org_a.base}/appointments/{shop_a.appt_id}", headers=org_a.headers,
                        json={"master_id": foreign_master})
    assert r.status_code == 404, r.text


async def test_patch_appointment_foreign_service_404(api, org_a, org_b, shop_a):
    foreign_service = await make_service(api, org_b, "Foreign")
    r = await api.patch(f"{org_a.base}/appointments/{shop_a.appt_id}", headers=org_a.headers,
                        json={"service_id": foreign_service})
    assert r.status_code == 404, r.text


# ---------- C2: нельзя удалять чужие отпуска / исключения графика ----------
async def test_cannot_delete_foreign_time_off(api, org_a, org_b):
    master_b = await make_master(api, org_b)
    r = await api.post(f"{org_b.base}/masters/{master_b}/time-off", headers=org_b.headers,
                       json={"start_date": "2030-02-01", "end_date": "2030-02-02", "reason": "vacation"})
    assert r.status_code == 201, r.text
    time_off_id = r.json()["id"]

    # организация A пытается удалить отпуск мастера организации B
    r = await api.delete(f"{org_a.base}/masters/{master_b}/time-off/{time_off_id}", headers=org_a.headers)
    assert r.status_code == 404, r.text

    # и он на месте у B
    r = await api.get(f"{org_b.base}/masters/{master_b}/time-off", headers=org_b.headers)
    assert len(r.json()) == 1


async def test_cannot_delete_foreign_schedule_exception(api, org_a, org_b):
    master_b = await make_master(api, org_b)
    r = await api.post(f"{org_b.base}/masters/{master_b}/schedule-exceptions", headers=org_b.headers,
                       json={"date": "2030-02-05", "start_time": "10:00", "end_time": "14:00"})
    assert r.status_code == 201, r.text
    exception_id = r.json()["id"]

    r = await api.delete(f"{org_a.base}/masters/{master_b}/schedule-exceptions/{exception_id}",
                         headers=org_a.headers)
    assert r.status_code == 404, r.text

    r = await api.get(f"{org_b.base}/masters/{master_b}/schedule-exceptions", headers=org_b.headers)
    assert len(r.json()) == 1


# ---------- Общая проверка: чужой org_id в URL ----------
async def test_cannot_access_other_org_by_url(api, org_a, org_b):
    r = await api.get(f"{org_b.base}/clients", headers=org_a.headers)
    assert r.status_code in (403, 404), r.text


# ---------- C4: публичный список мастеров и restore ----------
async def test_public_masters_with_services(api, org_a):
    service_id = await make_service(api, org_a)
    await make_master(api, org_a, service_ids=[service_id])

    r = await api.get(f"/api/public/{org_a.slug}/masters")
    assert r.status_code == 200, r.text
    data = r.json()
    assert len(data) == 1
    assert "services" in data[0]


async def test_restore_master_returns_services(api, org_a):
    service_id = await make_service(api, org_a)
    master_id = await make_master(api, org_a, service_ids=[service_id])

    r = await api.delete(f"{org_a.base}/masters/{master_id}", headers=org_a.headers)
    assert r.status_code == 204, r.text

    r = await api.post(f"{org_a.base}/masters/{master_id}/restore", headers=org_a.headers)
    assert r.status_code == 200, r.text
    assert "services" in r.json()