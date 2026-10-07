import logging
import re

import pytest

from observability import (RequestIdFilter, build_cors_origins, mask_secrets, request_id_var, scrub_sentry_event)

SECRET = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"


# ------------------------------------------------------------------ номер запроса

async def test_every_response_has_a_request_id(api):
    r = await api.get("/api/v1/currencies")
    assert r.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])

    other = await api.get("/api/v1/currencies")
    assert other.headers["x-request-id"] != r.headers["x-request-id"]


async def test_error_responses_have_a_request_id_too(api):
    r = await api.get("/api/v1/no-such-page")
    assert r.status_code == 404
    assert "x-request-id" in r.headers


async def test_client_request_id_is_kept_when_valid(api):
    r = await api.get("/api/v1/currencies", headers={"X-Request-ID": "frontend-req_123.abc"})
    assert r.headers["x-request-id"] == "frontend-req_123.abc"


@pytest.mark.parametrize("bad", ["short", "has spaces in it!", "x" * 65, "<script>alert(1)</script>"])
async def test_invalid_client_request_id_is_replaced(api, bad):
    r = await api.get("/api/v1/currencies", headers={"X-Request-ID": bad})
    assert r.headers["x-request-id"] != bad
    assert re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])


def test_log_records_get_the_request_id():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    token = request_id_var.set("req-12345678")
    try:
        assert RequestIdFilter().filter(record) is True
    finally:
        request_id_var.reset(token)
    assert record.request_id == "req-12345678"
    assert logging.Formatter("[%(request_id)s] %(message)s").format(record) == "[req-12345678] hello"


def test_log_records_outside_a_request_get_a_dash():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", None, None)
    RequestIdFilter().filter(record)
    assert record.request_id == "-"


# ------------------------------------------------------------------ журнал запросов и секреты

async def test_access_log_hides_secret_tokens(api, caplog):
    with caplog.at_level(logging.INFO, logger="access"):
        r = await api.get(f"/api/v1/public/booking/{SECRET}")
    assert r.status_code == 404
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "access"]
    assert len(lines) == 1
    assert "/api/v1/public/booking/{token}" in lines[0] and "404" in lines[0]
    assert SECRET not in lines[0]


async def test_access_log_does_not_contain_query_strings(api, caplog):
    with caplog.at_level(logging.INFO, logger="access"):
        await api.get("/api/v1/currencies?secret=hunter2")
    lines = [rec.getMessage() for rec in caplog.records if rec.name == "access"]  # журнал самого клиента httpx не в счёт
    assert len(lines) == 1 and "/api/v1/currencies" in lines[0]
    assert "hunter2" not in lines[0]


def test_mask_secrets():
    assert mask_secrets(f"/api/v1/public/booking/{SECRET}") == "/api/v1/public/booking/{token}"
    assert mask_secrets(f"/api/v1/public/booking/{SECRET}/cancel") == "/api/v1/public/booking/{token}/cancel"
    assert mask_secrets(f"https://api.example.com/api/v1/public/booking/{SECRET}/reschedule?x=1") == (
        "https://api.example.com/api/v1/public/booking/{token}/reschedule?x=1"
    )
    assert mask_secrets("/api/v1/currencies") == "/api/v1/currencies"


def test_sentry_events_lose_secret_tokens():
    event = {
        "request": {"url": f"https://api.example.com/api/v1/public/booking/{SECRET}/cancel"},
        "transaction": f"/api/v1/public/booking/{SECRET}",
    }
    cleaned = scrub_sentry_event(event, {})
    assert SECRET not in str(cleaned)
    assert cleaned["request"]["url"].endswith("/booking/{token}/cancel")


def test_sentry_scrubbing_tolerates_events_without_request():
    assert scrub_sentry_event({"message": "boom"}, {}) == {"message": "boom"}


# ------------------------------------------------------------------ CORS

def test_cors_origins_are_built_from_settings():
    assert build_cors_origins("http://localhost:4200", "https://app.example.com") == [
        "http://localhost:4200", "https://app.example.com",
    ]


def test_cors_origins_are_cleaned():
    origins = build_cors_origins(
        " https://a.example.com/ ,https://b.example.com,https://a.example.com, ,*,not-a-url", "https://app.example.com/login",
    )
    assert origins == ["https://a.example.com", "https://b.example.com", "https://app.example.com"]


def test_cors_never_allows_a_wildcard():
    assert "*" not in build_cors_origins("*", "https://app.example.com")


def test_cors_works_with_empty_extra_origins():
    assert build_cors_origins("", "https://app.example.com") == ["https://app.example.com"]


async def test_preflight_from_an_allowed_origin(api):
    r = await api.options("/api/v1/currencies", headers={
        "Origin": "http://localhost:4200",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert "x-request-id" in r.headers  # номер есть даже у ответа на preflight


async def test_preflight_from_a_foreign_origin_is_refused(api):
    r = await api.options("/api/v1/currencies", headers={
        "Origin": "https://evil.example.com",
        "Access-Control-Request-Method": "GET",
    })
    assert "access-control-allow-origin" not in r.headers


async def test_frontend_can_read_the_request_id_header(api):
    r = await api.get("/api/v1/currencies", headers={"Origin": "http://localhost:4200"})
    assert r.headers["access-control-allow-origin"] == "http://localhost:4200"
    assert "x-request-id" in r.headers["access-control-expose-headers"].lower()
