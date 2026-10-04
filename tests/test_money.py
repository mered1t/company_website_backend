async def test_currencies_endpoint(api):
    r = await api.get("/api/v1/currencies")
    assert r.status_code == 200
    by_code = {c["code"]: c for c in r.json()}
    assert set(by_code) == {"EUR", "USD", "ALL", "UAH"}
    assert by_code["EUR"]["minor_unit"] == 2


async def test_service_price_is_stored_in_minor_units(api, org_a):
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "Haircut", "price": 1250, "duration_minutes": 60})
    assert r.status_code == 201, r.text
    assert r.json()["price"] == 1250  # 12.50 в основной валюте


async def test_negative_or_absurd_price_is_rejected(api, org_a):
    for bad_price in (-1, 100_000_001):
        r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                           json={"name": "Bad", "price": bad_price, "duration_minutes": 60})
        assert r.status_code == 422, (bad_price, r.text)