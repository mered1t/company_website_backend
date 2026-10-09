"""Админка платформы: список организаций, платежи, ручные правки, права доступа."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

import models
from core.time_utils import utc_now
from domain.subscription import add_months
from tests.test_org_data import PASSWORD
from tests.test_subscription import DAY, _expired, _set, _trial

ADMIN_EMAIL = "platform@example.com"


@pytest.fixture
async def admin(api, db):
    r = await api.post("/api/v1/users", json={
        "username": "platform", "email": ADMIN_EMAIL, "password": PASSWORD, "accept_terms": True,
    })
    assert r.status_code in (200, 201), r.text
    r = await api.post("/api/v1/users/token", data={"username": ADMIN_EMAIL, "password": PASSWORD})
    assert r.status_code == 200, r.text
    user = (await db.execute(select(models.User).where(models.User.email == ADMIN_EMAIL))).scalar_one()
    user.is_platform_admin = True
    await db.commit()
    return SimpleNamespace(headers={"Authorization": f"Bearer {r.json()['access_token']}"}, id=user.id)


def pay(**overrides):
    body = dict(amount=1500, currency="EUR", method="cash", plan="basic", months=1)
    body.update(overrides)
    return body


async def _org(db, org_id):
    return (await db.execute(
        select(models.Organization).where(models.Organization.id == org_id).execution_options(populate_existing=True),
    )).scalar_one()


async def test_only_platform_admins_get_in(api, org_a):
    url = f"/api/v1/admin/organizations/{org_a.org_id}"
    for method, path, kwargs in [
        ("get", "/api/v1/admin/organizations", {}),
        ("get", url, {}),
        ("patch", url, {"json": {"is_free": True}}),
        ("post", f"{url}/payments", {"json": pay()}),
    ]:
        assert (await getattr(api, method)(path, **kwargs)).status_code == 401, path
        r = await getattr(api, method)(path, headers=org_a.headers, **kwargs)  # владелец салона, но не админ платформы
        assert r.status_code == 403, (path, r.text)


async def test_list_shows_owner_and_status(api, org_a, org_b, admin):
    r = await api.get("/api/v1/admin/organizations", headers=admin.headers)
    assert r.status_code == 200, r.text
    rows = {o["id"]: o for o in r.json()}
    assert rows[org_a.org_id]["owner_email"] == "ownera@example.com"
    assert rows[org_a.org_id]["subscription"]["status"] == "active"
    assert rows[org_a.org_id]["plan"] == "basic"
    assert [o["id"] for o in r.json()] == sorted(rows, reverse=True)


async def test_list_filters_and_pages(api, db, org_a, org_b, admin):
    await _set(db, org_b.org_id, **_trial())
    url = "/api/v1/admin/organizations"

    r = await api.get(url, params={"status": "trial"}, headers=admin.headers)
    assert [o["id"] for o in r.json()] == [org_b.org_id]
    r = await api.get(url, params={"status": "active"}, headers=admin.headers)
    assert [o["id"] for o in r.json()] == [org_a.org_id]
    assert (await api.get(url, params={"status": "nonsense"}, headers=admin.headers)).status_code == 422

    r = await api.get(url, params={"q": "ownera"}, headers=admin.headers)  # по email владельца
    assert [o["id"] for o in r.json()] == [org_a.org_id]
    r = await api.get(url, params={"q": "Shop"}, headers=admin.headers)  # по названию
    assert len(r.json()) == 2
    r = await api.get(url, params={"q": "%"}, headers=admin.headers)  # спецсимволы не работают как маска
    assert r.json() == []

    first = await api.get(url, params={"limit": 1}, headers=admin.headers)
    second = await api.get(url, params={"limit": 1, "skip": 1}, headers=admin.headers)
    assert len(first.json()) == len(second.json()) == 1
    assert first.json()[0]["id"] != second.json()[0]["id"]


async def test_payment_extends_from_the_paid_period_end(api, db, org_a, admin):
    before = (await _org(db, org_a.org_id)).paid_until
    r = await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=pay(months=3), headers=admin.headers)
    assert r.status_code == 201, r.text
    org = await _org(db, org_a.org_id)
    assert org.paid_until == add_months(before, 3)
    assert r.json()["payment"]["period_start"] == before.isoformat()
    assert r.json()["organization"]["subscription"]["status"] == "active"


async def test_payment_during_the_trial_keeps_the_remaining_trial_days(api, db, org_a, admin):
    await _set(db, org_a.org_id, **_trial())
    trial_end = (await _org(db, org_a.org_id)).trial_ends_at
    r = await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=pay(), headers=admin.headers)
    assert r.status_code == 201, r.text
    assert (await _org(db, org_a.org_id)).paid_until == add_months(trial_end, 1)


@pytest.mark.parametrize("state", ["expired", "grace"])
async def test_payment_after_the_period_started_counts_from_today(api, db, org_a, admin, state):
    await _set(db, org_a.org_id, paid_until=None, trial_ends_at=utc_now() - (8 if state == "expired" else 1) * DAY)
    r = await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=pay(), headers=admin.headers)
    assert r.status_code == 201, r.text
    org = await _org(db, org_a.org_id)
    assert abs(org.paid_until - add_months(utc_now(), 1)) < timedelta(seconds=30)
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 200


async def test_payments_stack_and_the_plan_follows_the_last_payment(api, db, org_a, admin):
    url = f"/api/v1/admin/organizations/{org_a.org_id}/payments"
    first = (await api.post(url, json=pay(), headers=admin.headers)).json()["payment"]
    second = (await api.post(url, json=pay(plan="pro", months=2), headers=admin.headers)).json()["payment"]
    assert second["period_start"] == first["period_end"]
    org = await _org(db, org_a.org_id)
    assert org.plan == "pro"
    assert org.paid_until.isoformat() == second["period_end"]
    assert second["period_end"] == add_months(datetime.fromisoformat(first["period_end"]), 2).isoformat()

    usage = await api.get(f"{org_a.base}/analytics/ai-usage", headers=org_a.headers)
    assert usage.json()["ai_enabled"] is True


async def test_payment_record_has_the_details(api, db, org_a, admin):
    r = await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", headers=admin.headers,
                       json=pay(currency="eur", method="bank_transfer", paid_at="2030-02-03", note="founder price"))
    assert r.status_code == 201, r.text
    p = r.json()["payment"]
    assert p["recorded_by"] == admin.id
    assert p["organization_name"] == "Shop ownera"
    assert p["currency"] == "EUR" and p["method"] == "bank_transfer" and p["note"] == "founder price"
    assert p["paid_at"].startswith("2030-02-03")


async def test_free_period_is_a_zero_amount_grant(api, db, org_a, admin):
    await _set(db, org_a.org_id, **_expired())
    url = f"/api/v1/admin/organizations/{org_a.org_id}/payments"
    r = await api.post(url, json=pay(amount=0, method="grant", months=3, note="pilot"), headers=admin.headers)
    assert r.status_code == 201, r.text
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 200


@pytest.mark.parametrize("body", [
    pay(method="grant", amount=100),        # бесплатный период с суммой
    pay(amount=0),                          # платёж без суммы
    pay(months=0), pay(months=37), pay(amount=-1),
    pay(currency="XXX"), pay(method="bitcoin"), pay(plan="gold"),
])
async def test_bad_payments_are_rejected(api, db, org_a, admin, body):
    r = await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=body, headers=admin.headers)
    assert r.status_code == 422, r.text
    assert (await db.execute(select(models.Payment))).scalars().all() == []


async def test_unknown_organization_is_404(api, admin):
    for call in (
        api.get("/api/v1/admin/organizations/99999", headers=admin.headers),
        api.patch("/api/v1/admin/organizations/99999", json={"is_free": True}, headers=admin.headers),
        api.post("/api/v1/admin/organizations/99999/payments", json=pay(), headers=admin.headers),
    ):
        assert (await call).status_code == 404


async def test_detail_lists_payments_newest_first(api, org_a, admin):
    url = f"/api/v1/admin/organizations/{org_a.org_id}"
    await api.post(f"{url}/payments", json=pay(paid_at="2030-01-01"), headers=admin.headers)
    await api.post(f"{url}/payments", json=pay(paid_at="2030-03-01"), headers=admin.headers)
    r = await api.get(url, headers=admin.headers)
    assert r.status_code == 200
    assert [p["paid_at"][:10] for p in r.json()["payments"]] == ["2030-03-01", "2030-01-01"]
    assert r.json()["subscription"]["status"] == "active"


async def test_block_and_unblock(api, db, org_a, admin):
    url = f"/api/v1/admin/organizations/{org_a.org_id}"
    r = await api.patch(url, json={"is_blocked": True}, headers=admin.headers)
    assert r.status_code == 200 and r.json()["subscription"]["status"] == "blocked"
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 403

    await api.patch(url, json={"is_blocked": False}, headers=admin.headers)
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 200


async def test_payment_does_not_lift_a_block(api, db, org_a, admin):
    await api.patch(f"/api/v1/admin/organizations/{org_a.org_id}", json={"is_blocked": True}, headers=admin.headers)
    await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=pay(), headers=admin.headers)
    assert (await api.get(f"{org_a.base}/appointments", headers=org_a.headers)).status_code == 403


async def test_grant_free_access_and_change_plan(api, db, org_a, admin):
    await _set(db, org_a.org_id, **_expired())
    r = await api.patch(f"/api/v1/admin/organizations/{org_a.org_id}", headers=admin.headers,
                        json={"is_free": True, "plan": "pro", "billing_note": "own demo"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["subscription"]["status"] == "free" and body["plan"] == "pro" and body["billing_note"] == "own demo"
    assert body["subscription"]["ai_enabled"] is True


async def test_dates_can_be_corrected_by_hand(api, db, org_a, admin):
    url = f"/api/v1/admin/organizations/{org_a.org_id}"
    r = await api.patch(url, json={"paid_until": "2031-01-01T02:00:00+02:00"}, headers=admin.headers)
    assert r.status_code == 200, r.text
    assert (await _org(db, org_a.org_id)).paid_until.isoformat() == "2031-01-01T00:00:00"  # переведено в UTC

    r = await api.patch(url, json={"paid_until": None, "trial_ends_at": "2031-06-01T00:00:00"}, headers=admin.headers)
    assert r.status_code == 200, r.text
    org = await _org(db, org_a.org_id)
    assert org.paid_until is None and org.trial_ends_at.isoformat() == "2031-06-01T00:00:00"
    assert r.json()["subscription"]["status"] == "trial"

    await api.patch(url, json={"billing_note": "x"}, headers=admin.headers)
    r = await api.patch(url, json={"billing_note": None}, headers=admin.headers)
    assert r.json()["billing_note"] is None


@pytest.mark.parametrize("body", [
    {"trial_ends_at": None}, {"plan": None}, {"is_free": None}, {"is_blocked": None}, {"plan": "gold"},
])
async def test_patch_rejects_nulls_and_unknown_plans(api, org_a, admin, body):
    r = await api.patch(f"/api/v1/admin/organizations/{org_a.org_id}", json=body, headers=admin.headers)
    assert r.status_code == 422, r.text


async def test_patch_changes_only_what_was_sent(api, db, org_a, admin):
    before = await _org(db, org_a.org_id)
    paid, trial = before.paid_until, before.trial_ends_at
    r = await api.patch(f"/api/v1/admin/organizations/{org_a.org_id}", json={"billing_note": "hi"}, headers=admin.headers)
    assert r.status_code == 200
    after = await _org(db, org_a.org_id)
    assert (after.paid_until, after.trial_ends_at, after.plan, after.is_free) == (paid, trial, "basic", False)


async def test_payments_stay_after_the_organization_is_deleted(api, db, org_a, admin):
    await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=pay(), headers=admin.headers)
    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers,
                       json={"password": PASSWORD, "confirm_name": "Shop ownera"})
    assert r.status_code == 204, r.text

    (payment,) = (await db.execute(select(models.Payment))).scalars().all()
    assert payment.organization_id is None
    assert payment.organization_name == "Shop ownera" and payment.amount == 1500


async def test_payments_stay_after_the_admin_deletes_their_account(api, db, org_a, admin):
    await api.post(f"/api/v1/admin/organizations/{org_a.org_id}/payments", json=pay(), headers=admin.headers)
    r = await api.post("/api/v1/users/me/delete", headers=admin.headers, json={"password": PASSWORD})
    assert r.status_code == 204, r.text
    (payment,) = (await db.execute(select(models.Payment).execution_options(populate_existing=True))).scalars().all()
    assert payment.recorded_by is None
