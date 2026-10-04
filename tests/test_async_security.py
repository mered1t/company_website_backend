import asyncio

import pytest

from auth.auth import DUMMY_PASSWORD_HASH, hash_password_async, verify_password_async


@pytest.mark.asyncio
async def test_password_hashing_does_not_block_event_loop():
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    task = asyncio.create_task(ticker())
    await asyncio.gather(*(hash_password_async("Some-password-1") for _ in range(3)))
    task.cancel()
    # если бы хеширование блокировало loop, тиков почти не было бы
    assert ticks >= 3


@pytest.mark.asyncio
async def test_verify_password_async_roundtrip():
    hashed = await hash_password_async("Correct-horse-1")
    assert await verify_password_async("Correct-horse-1", hashed) is True
    assert await verify_password_async("wrong-password", hashed) is False


@pytest.mark.asyncio
async def test_login_checks_hash_even_for_unknown_email(api, monkeypatch):
    calls = []

    async def fake_verify(plain, hashed):
        calls.append(hashed)
        return False

    monkeypatch.setattr("routers.users.verify_password_async", fake_verify)

    resp = await api.post(
        "/api/v1/users/token",
        data={"username": "nobody@example.com", "password": "whatever-123"},
    )
    assert resp.status_code == 401
    assert calls == [DUMMY_PASSWORD_HASH]