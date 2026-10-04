"""Границы входных данных: слишком длинное/пустое/бессмысленное отклоняется с 422."""


async def test_password_length_is_limited(api):
    r = await api.post("/api/v1/users", json={
        "username": "longpass", "email": "longpass@example.com",
        "password": "Aa1" + "a" * 130, "accept_terms": True,
    })
    assert r.status_code == 422, r.text

    r = await api.post("/api/v1/users/token", data={"username": "x@example.com", "password": "x" * 5000})
    assert r.status_code == 401, r.text


async def test_service_duration_is_limited(api, org_a):
    url = f"{org_a.base}/services"
    for minutes, expected in ((1440, 201), (1441, 422), (10**9, 422)):
        r = await api.post(url, headers=org_a.headers,
                           json={"name": f"S{minutes}", "duration_minutes": minutes, "price": 100})
        assert r.status_code == expected, f"{minutes}: {r.status_code} {r.text}"


async def test_names_are_trimmed_and_cannot_be_blank(api, org_a):
    r = await api.post(f"{org_a.base}/clients", headers=org_a.headers,
                       json={"full_name": "   ", "phone": "+380990000001"})
    assert r.status_code == 422, r.text

    r = await api.post(f"{org_a.base}/clients", headers=org_a.headers,
                       json={"full_name": "  Anna  ", "phone": "+380990000002"})
    assert r.status_code == 201, r.text
    assert r.json()["full_name"] == "Anna"


async def test_client_notes_and_birth_date_limits(api, org_a):
    url = f"{org_a.base}/clients"
    base = {"full_name": "Test", "phone": "+380990000001"}

    r = await api.post(url, headers=org_a.headers, json={**base, "notes": "x" * 2001})
    assert r.status_code == 422, r.text
    r = await api.post(url, headers=org_a.headers, json={**base, "birth_date": "2999-01-01"})
    assert r.status_code == 422, r.text
    r = await api.post(url, headers=org_a.headers, json={**base, "birth_date": "1850-01-01"})
    assert r.status_code == 422, r.text

    r = await api.post(url, headers=org_a.headers,
                       json={**base, "notes": "x" * 2000, "birth_date": "1990-05-17"})
    assert r.status_code == 201, r.text

    client_id = r.json()["id"]
    r = await api.patch(f"{url}/{client_id}", headers=org_a.headers, json={"birth_date": "2999-01-01"})
    assert r.status_code == 422, r.text


async def test_booking_horizon_days_range(api, org_a):
    for days, expected in ((0, 422), (366, 422), (30, 200)):
        r = await api.patch(org_a.base, headers=org_a.headers, json={"booking_horizon_days": days})
        assert r.status_code == expected, f"{days}: {r.status_code} {r.text}"