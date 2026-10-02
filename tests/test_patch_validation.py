"""PATCH: обязательные поля нельзя обнулить; необязательные по-прежнему можно очистить."""
from tests.test_tenant_isolation import make_appointment, make_client, make_master, make_service


async def test_patch_rejects_explicit_null_for_required_fields(api, db, org_a):
    service_id = await make_service(api, org_a)
    client_id = await make_client(api, org_a, "+380990000001")
    master_id = await make_master(api, org_a)
    appt_id = await make_appointment(db, org_a, client_id, service_id, master_id)

    cases = [
        (f"/clients/{client_id}", "full_name"),
        (f"/clients/{client_id}", "phone"),
        (f"/services/{service_id}", "name"),
        (f"/services/{service_id}", "price"),
        (f"/services/{service_id}", "duration_minutes"),
        (f"/masters/{master_id}", "full_name"),
        (f"/appointments/{appt_id}", "client_id"),
        (f"/appointments/{appt_id}", "service_id"),
        (f"/appointments/{appt_id}", "master_id"),
        (f"/appointments/{appt_id}", "start_time"),
        (f"/appointments/{appt_id}", "status"),
        ("", "name"),
        ("", "timezone"),
        ("", "booking_horizon_days"),
        ("", "slug"),
    ]
    for path, field in cases:
        r = await api.patch(f"{org_a.base}{path}", headers=org_a.headers, json={field: None})
        assert r.status_code == 422, f"PATCH {path or '/'} {field}=null → {r.status_code} {r.text}"


async def test_patch_can_still_clear_optional_fields(api, org_a):
    client_id = await make_client(api, org_a, "+380990000001")
    url = f"{org_a.base}/clients/{client_id}"

    r = await api.patch(url, headers=org_a.headers, json={"notes": "vip", "email": "a@example.com"})
    assert r.status_code == 200, r.text

    r = await api.patch(url, headers=org_a.headers, json={"notes": None, "email": None})
    assert r.status_code == 200, r.text
    assert r.json()["notes"] is None and r.json()["email"] is None

    r = await api.patch(url, headers=org_a.headers, json={})   # пустой PATCH ничего не ломает
    assert r.status_code == 200, r.text