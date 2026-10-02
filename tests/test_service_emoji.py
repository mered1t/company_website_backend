"""У услуги есть эмодзи вместо фото: сохраняется, отдаётся (в том числе публично), проверяется."""


async def _create(api, org, **extra):
    return await api.post(f"{org.base}/services", headers=org.headers,
                          json={"name": "Haircut", "duration_minutes": 30, "price": 1000, **extra})


async def test_emoji_is_saved_and_returned_publicly(api, org_a):
    r = await _create(api, org_a, emoji="✂️")
    assert r.status_code == 201, r.text
    assert r.json()["emoji"] == "✂️"

    r = await api.get(f"/api/public/{org_a.slug}/services")
    assert r.status_code == 200, r.text
    assert r.json()[0]["emoji"] == "✂️"


async def test_complex_emoji_are_accepted(api, org_a):
    for i, emoji in enumerate(["💈", "👩‍🦰", "🇺🇦", "👨‍👩‍👧‍👦"]):
        r = await api.post(f"{org_a.base}/services", headers=org_a.headers,
                           json={"name": f"S{i}", "duration_minutes": 30, "price": 100, "emoji": emoji})
        assert r.status_code == 201, f"{emoji}: {r.text}"


async def test_non_emoji_values_are_rejected(api, org_a):
    for bad in ["abc", "1", " ", "✂️ ", "", "💈" * 17]:
        r = await _create(api, org_a, emoji=bad)
        assert r.status_code == 422, f"{bad!r}: {r.status_code}"


async def test_emoji_can_be_changed_and_cleared(api, org_a):
    service_id = (await _create(api, org_a)).json()["id"]
    url = f"{org_a.base}/services/{service_id}"
    assert (await api.get(url, headers=org_a.headers)).json()["emoji"] is None

    r = await api.patch(url, headers=org_a.headers, json={"emoji": "💈"})
    assert r.status_code == 200 and r.json()["emoji"] == "💈", r.text

    r = await api.patch(url, headers=org_a.headers, json={"emoji": None})
    assert r.status_code == 200 and r.json()["emoji"] is None, r.text

    r = await api.patch(url, headers=org_a.headers, json={"emoji": "abc"})
    assert r.status_code == 422, r.text