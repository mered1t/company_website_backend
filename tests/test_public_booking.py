from datetime import datetime, timedelta

from tests.test_tenant_isolation import make_master, make_service

NOT_PROVIDED = "This master does not provide this service"


def _tomorrow() -> datetime:
    return datetime.now() + timedelta(days=1)


def _booking(service_id, master_id):
    start = _tomorrow().replace(hour=12, minute=0, second=0, microsecond=0)
    return {
        "client_full_name": "Test Client",
        "client_phone": "+380991234567",
        "service_id": service_id,
        "master_id": master_id,
        "start_time": start.isoformat(),
    }


async def _shop(api, org):
    service_a = await make_service(api, org, "Haircut")
    service_b = await make_service(api, org, "Coloring")
    master = await make_master(api, org, service_ids=[service_a])  # делает только Haircut
    return service_a, service_b, master


async def test_public_booking_rejects_service_master_does_not_provide(api, org_a):
    _, service_b, master = await _shop(api, org_a)
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=_booking(service_b, master))
    assert r.status_code == 400
    assert r.json()["detail"] == NOT_PROVIDED


async def test_public_slots_reject_service_master_does_not_provide(api, org_a):
    _, service_b, master = await _shop(api, org_a)
    day = _tomorrow().date().isoformat()
    r = await api.get(f"/api/v1/public/{org_a.slug}/available-slots",
                      params={"master_id": master, "service_id": service_b, "date": day})
    assert r.status_code == 400
    assert r.json()["detail"] == NOT_PROVIDED


async def test_public_dates_reject_service_master_does_not_provide(api, org_a):
    _, service_b, master = await _shop(api, org_a)
    month = _tomorrow().strftime("%Y-%m")
    r = await api.get(f"/api/v1/public/{org_a.slug}/available-dates",
                      params={"master_id": master, "service_id": service_b, "month": month})
    assert r.status_code == 400
    assert r.json()["detail"] == NOT_PROVIDED


async def test_master_with_matching_or_no_services_passes_this_check(api, org_a):
    service_a, service_b, master = await _shop(api, org_a)
    generalist = await make_master(api, org_a)  # без списка услуг = делает любые

    # ответ может быть другим (например, нет рабочих часов), но не по причине услуги
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=_booking(service_a, master))
    assert r.json().get("detail") != NOT_PROVIDED
    r = await api.post(f"/api/v1/public/{org_a.slug}/book", json=_booking(service_b, generalist))
    assert r.json().get("detail") != NOT_PROVIDED