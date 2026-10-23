"""Запросы фронта от 23.10: group_bookings в списке салонов, GET одного салона, коды стартовых статусов,
кэш и лимит автоперевода услуг."""
import pytest
from limits import parse

import services.translations as tr
from core.rate_limiter import limiter
from services.client_statuses import DEFAULT_CODES, DEFAULT_NAMES


# ------------------------------------------------------------------ 1 и 4: настройки салона
async def test_list_and_single_show_saved_settings(api, org_a, org_b):
    r = await api.patch(org_a.base, headers=org_a.headers,
                        json={"group_bookings": True, "business_type": "sports", "auto_complete": "after_2h"})
    assert r.status_code == 200
    listed = next(o for o in (await api.get("/api/v1/organizations", headers=org_a.headers)).json()
                  if o["id"] == org_a.org_id)
    assert (listed["group_bookings"], listed["business_type"], listed["auto_complete"]) == (True, "sports", "after_2h")
    assert set(r.json()) <= set(listed)  # в списке есть всё, что отдаёт PATCH

    one = await api.get(org_a.base, headers=org_a.headers)
    assert one.status_code == 200 and one.json() == listed
    assert one.json()["role"] == "owner" and one.json()["subscription"]["status"]
    assert (await api.get(org_b.base, headers=org_a.headers)).status_code in (403, 404)
    assert (await api.get(org_a.base)).status_code == 401


# ------------------------------------------------------------------ 2: коды стартовых статусов
async def test_default_statuses_have_codes(api, org_a):
    url = f"{org_a.base}/client-statuses"
    rows = (await api.get(url, headers=org_a.headers)).json()
    assert [s["code"] for s in rows] == list(DEFAULT_CODES)

    vip = rows[2]
    recolored = await api.patch(f"{url}/{vip['id']}", headers=org_a.headers, json={"color": "#000000"})
    assert recolored.json()["code"] == "vip"  # цвет не трогает код
    same_name = await api.patch(f"{url}/{vip['id']}", headers=org_a.headers, json={"name": vip["name"]})
    assert same_name.json()["code"] == "vip"  # та же форма без изменений — код остаётся
    renamed = await api.patch(f"{url}/{vip['id']}", headers=org_a.headers, json={"name": "Золото"})
    assert renamed.json()["code"] is None  # своё название: переводить больше не нужно
    own = await api.post(url, headers=org_a.headers, json={"name": "Абонемент"})
    assert own.json()["code"] is None

    client = await api.post(f"{org_a.base}/clients", headers=org_a.headers,
                            json={"full_name": "Anna", "phone": "+380670001501", "status_id": rows[1]["id"]})
    assert client.json()["status"]["code"] == "regular"


def test_every_language_has_all_default_names():
    assert all(len(names) == len(DEFAULT_CODES) for names in DEFAULT_NAMES.values())


# ------------------------------------------------------------------ 3: кэш и лимит автоперевода
@pytest.fixture
def ai(monkeypatch):
    calls = []

    async def fake(payload):
        calls.append(sorted(payload["targets"]))
        return {lang: {"name": f"{payload['name']}-{lang}", "description": None} for lang in payload["targets"]}

    monkeypatch.setattr(tr, "call_translation_model", fake)
    return calls


async def test_only_requested_languages_and_cache(api, org_a, org_b, ai):
    url = f"{org_a.base}/services/translate"
    r = await api.post(url, headers=org_a.headers, json={"name": "Стрижка", "target_languages": ["pl", "pl", "es"]})
    assert r.status_code == 200
    assert r.json() == {"translations": {"pl": {"name": "Стрижка-pl", "description": None},
                                         "es": {"name": "Стрижка-es", "description": None}}}
    assert ai == [["es", "pl"]]  # только запрошенные языки, без повторов

    # тот же текст (другие пробелы) в другом салоне: из кэша, ИИ не вызывается
    other = await api.post(f"{org_b.base}/services/translate", headers=org_b.headers,
                           json={"name": "  Стрижка ", "target_languages": ["es"]})
    assert other.json()["translations"]["es"]["name"] == "Стрижка-es" and len(ai) == 1

    # часть есть в кэше: в ИИ уходит только недостающий язык
    r = await api.post(url, headers=org_a.headers, json={"name": "Стрижка", "target_languages": ["pl", "en"]})
    assert set(r.json()["translations"]) == {"pl", "en"} and ai[-1] == ["en"]
    assert r.headers["x-ratelimit-limit"] == "30" and int(r.headers["x-ratelimit-remaining"]) <= 30

    # другой текст или описание — другой ключ
    await api.post(url, headers=org_a.headers, json={"name": "Стрижка", "description": "Миття",
                                                     "target_languages": ["pl"]})
    assert ai[-1] == ["pl"] and len(ai) == 3


async def test_limit_counts_only_ai_calls(api, org_a, org_b, ai, monkeypatch):
    monkeypatch.setattr(tr, "TRANSLATE_LIMIT", parse("2/hour"))
    limiter.reset()
    previous, limiter.enabled = limiter.enabled, True
    try:
        url = f"{org_a.base}/services/translate"
        first = await api.post(url, headers=org_a.headers, json={"name": "A", "target_languages": ["pl"]})
        assert first.headers["x-ratelimit-remaining"] == "1"
        cached = await api.post(url, headers=org_a.headers, json={"name": "A", "target_languages": ["pl"]})
        assert cached.status_code == 200 and cached.headers["x-ratelimit-remaining"] == "1"  # из кэша — бесплатно
        assert (await api.post(url, headers=org_a.headers, json={"name": "B", "target_languages": ["pl"]})
                ).headers["x-ratelimit-remaining"] == "0"

        over = await api.post(url, headers=org_a.headers, json={"name": "C", "target_languages": ["pl"]})
        assert over.status_code == 429 and int(over.headers["retry-after"]) > 0
        assert over.headers["x-ratelimit-remaining"] == "0" and len(ai) == 2
        # из кэша можно и после исчерпания лимита; у другого салона свой лимит
        assert (await api.post(url, headers=org_a.headers, json={"name": "A", "target_languages": ["pl"]})
                ).status_code == 200
        assert (await api.post(f"{org_b.base}/services/translate", headers=org_b.headers,
                               json={"name": "C", "target_languages": ["pl"]})).status_code == 200
    finally:
        limiter.enabled = previous
        limiter.reset()


async def test_text_length_is_limited(api, org_a, ai):
    url = f"{org_a.base}/services/translate"
    assert (await api.post(url, headers=org_a.headers, json={"name": "x" * 151})).status_code == 422
    assert (await api.post(url, headers=org_a.headers,
                           json={"name": "X", "description": "y" * 2001})).status_code == 422
    assert ai == []


async def test_frontend_can_read_the_limit_headers(api):
    r = await api.get("/api/v1/currencies", headers={"Origin": "http://localhost:4200"})
    exposed = r.headers["access-control-expose-headers"].lower()
    assert all(h in exposed for h in ("x-ratelimit-remaining", "x-ratelimit-limit", "retry-after"))
