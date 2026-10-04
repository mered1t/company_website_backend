async def test_public_masters_do_not_expose_phone(api, org_a):
    r = await api.post(
        f"{org_a.base}/masters",
        json={"full_name": "Barber Bob", "phone": "+380501234567"},
        headers=org_a.headers,
    )
    assert r.status_code in (200, 201), r.text

    r = await api.get(f"/api/v1/public/{org_a.slug}/masters")
    assert r.status_code == 200, r.text
    masters = r.json()
    assert masters, "публичный список мастеров пуст"
    assert masters[0]["full_name"] == "Barber Bob"
    assert "phone" not in masters[0]
    assert "created_at" not in masters[0]

    # в админском API телефон остаётся
    r = await api.get(f"{org_a.base}/masters", headers=org_a.headers)
    assert r.status_code == 200, r.text
    assert r.json()[0]["phone"] == "+380501234567"
