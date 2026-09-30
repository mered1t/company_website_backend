from types import SimpleNamespace

from rate_limiter import get_client_ip


def make_request(headers, host="10.0.0.1"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=host))


def test_uses_cloudflare_header():
    req = make_request({"cf-connecting-ip": "217.21.1.1", "x-forwarded-for": "1.2.3.4"})
    assert get_client_ip(req) == "217.21.1.1"


def test_ignores_spoofed_x_forwarded_for():
    req = make_request({"x-forwarded-for": "1.2.3.4"}, host="10.0.0.1")
    assert get_client_ip(req) == "10.0.0.1"


def test_falls_back_to_client_host_locally():
    req = make_request({}, host="127.0.0.1")
    assert get_client_ip(req) == "127.0.0.1"