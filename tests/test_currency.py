from datetime import datetime

import models
from tests.test_invitations import make_invitation, register_and_login, verify_in_db
from tests.test_tenant_isolation import make_client, make_master, make_service

PERIOD = {"date_from": "2030-01-01T00:00:00", "date_to": "2030-12-31T23:59:59"}


async def test_new_organization_defaults_to_eur(api, org_a):
    r = await api.get("/api/organizations", headers=org_a.headers)
    assert r.json()[0]["currency"] == "EUR"


async def test_create_organization_with_currency(api, org_a):
    r = await api.post("/api/organizations", headers=org_a.headers, json={"name": "Lek Shop", "currency": "ALL"})
    assert r.status_code == 201, r.text
    assert r.json()["currency"] == "ALL"


async def test_unsupported_currency_is_rejected(api, org_a):
    r = await api.post("/api/organizations", headers=org_a.headers, json={"name": "Pound Shop", "currency": "GBP"})
    assert r.status_code == 422


async def test_owner_can_change_currency_and_case_is_normalized(api, org_a):
    r = await api.patch(f"/api/organizations/{org_a.org_id}", headers=org_a.headers, json={"currency": "usd"})
    assert r.status_code == 200, r.text
    assert r.json()["currency"] == "USD"


async def test_currency_cannot_be_null(api, org_a):
    r = await api.patch(f"/api/organizations/{org_a.org_id}", headers=org_a.headers, json={"currency": None})
    assert r.status_code == 422


async def test_admin_cannot_change_currency(api, db, org_a):
    headers, _ = await register_and_login(api, "ivan", "ivan@example.com")
    await verify_in_db(db, "ivan@example.com")
    inv_id = await make_invitation(db, org_a.org_id, "ivan@example.com")
    assert (await api.post(f"/api/invitations/{inv_id}/accept", headers=headers)).status_code == 204

    r = await api.patch(f"/api/organizations/{org_a.org_id}", headers=headers, json={"currency": "USD"})
    assert r.status_code == 403, r.text


async def test_revenue_counts_only_current_currency(api, db, org_a):
    service_id = await make_service(api, org_a)
    client_id = await make_client(api, org_a, "+380990000040")
    master_id = await make_master(api, org_a)
    for hour, price, currency in [(10, 100, "EUR"), (12, 50, "USD")]:
        db.add(models.Appointment(
            organization_id=org_a.org_id, client_id=client_id, service_id=service_id, master_id=master_id,
            start_time=datetime(2030, 1, 7, hour), end_time=datetime(2030, 1, 7, hour + 1),
            status="completed", price=price, currency=currency,
        ))
    await db.commit()

    r = await api.get(f"{org_a.base}/analytics/revenue", headers=org_a.headers, params=PERIOD)
    assert r.status_code == 200, r.text
    assert r.json()["total_revenue"] == 100  # организация в EUR, доллары не суммируются


async def test_public_organization_info_has_currency(api, org_a):
    r = await api.get(f"/api/public/{org_a.slug}")
    assert r.status_code == 200, r.text
    assert r.json()["currency"] == "EUR"