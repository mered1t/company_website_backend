"""Сбой отправки письма не ломает запрос, но ошибка записывается в лог (а значит, видна в Sentry)."""
import logging

import routers.organizations as org_router
from tests.test_invitations import verify_in_db


def _boom(*args, **kwargs):
    raise RuntimeError("resend is down")


async def test_invitation_email_failure_is_logged_not_swallowed(api, db, org_a, monkeypatch, caplog):
    await verify_in_db(db, "ownera@example.com")
    monkeypatch.setattr(org_router, "send_invitation_email", _boom)

    with caplog.at_level(logging.ERROR):
        r = await api.post(f"{org_a.base}/invitations", headers=org_a.headers,
                           json={"email": "newperson@example.com", "role": "admin"})
        assert r.status_code == 201, r.text

        r = await api.post(f"{org_a.base}/invitations/{r.json()['id']}/resend", headers=org_a.headers)
        assert r.status_code == 204, r.text

    errors = [rec for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(errors) == 2
    assert all(rec.exc_info for rec in errors)               # трассировка ошибки сохранена
    assert all("newperson@example.com" not in rec.getMessage() for rec in errors)  # почты в логе нет