async def test_login_route_exists_under_v1_and_legacy(api):
    body = {"username": "nobody@example.com", "password": "wrong-password"}
    v1 = await api.post("/api/v1/users/token", data=body)
    legacy = await api.post("/api/users/token", data=body)
    assert v1.status_code != 404
    assert legacy.status_code != 404
    assert v1.status_code == legacy.status_code


async def test_health_stays_unversioned(api):
    r = await api.get("/health")
    assert r.status_code == 200


async def test_openapi_lists_only_v1_paths(api):
    r = await api.get("/openapi.json")
    assert r.status_code == 200
    paths = list(r.json()["paths"])
    assert paths
    assert all(p.startswith("/api/v1/") or p == "/health" for p in paths)
