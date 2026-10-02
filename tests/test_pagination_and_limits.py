"""Страницы не дублируют и не теряют записи; лимиты схем не превышают размеры колонок в базе."""
from tests.test_tenant_isolation import make_client


async def test_client_pages_have_no_duplicates_or_gaps(api, org_a):
    ids = [await make_client(api, org_a, f"+38099000000{i}") for i in range(5)]

    seen = []
    for skip in (0, 2, 4):
        r = await api.get(f"{org_a.base}/clients?skip={skip}&limit=2", headers=org_a.headers)
        assert r.status_code == 200, r.text
        seen += [c["id"] for c in r.json()]

    assert seen == sorted(ids)   # все пять, без повторов, в порядке создания


async def test_limits_match_database_columns(api, org_a):
    # photo в базе String(255)
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "S", "duration_minutes": 30, "price": 100, "photo": "x" * 256})
    assert r.status_code == 422, r.text
    r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                       json={"name": "S", "duration_minutes": 30, "price": 100, "photo": "x" * 255})
    assert r.status_code == 201, r.text

    # email клиента в базе String(120)
    long_email = "a" * 110 + "@example.com"   # 122 символа
    r = await api.post(f"{org_a.base}/clients", headers=org_a.headers,
                       json={"full_name": "T", "phone": "+380990000001", "email": long_email})
    assert r.status_code == 422, r.text