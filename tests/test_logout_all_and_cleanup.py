"""Выход со всех устройств и ежедневная чистка базы от устаревших строк."""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

import models
from core.config import settings
from core.rate_limiter import limiter
from core.time_utils import utc_now
from services.cleanup import purge_old_rows

PASSWORD = "Passw0rd!"
USERS = "/api/v1/users"


@pytest.fixture(autouse=True)
def _no_rate_limit():
    previous = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = previous


async def _tokens(api, name):
    email = f"{name}@example.com"
    r = await api.post(USERS, json={"username": name, "email": email, "password": PASSWORD, "accept_terms": True})
    assert r.status_code == 201, r.text
    r = await api.post(f"{USERS}/token", data={"username": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return email, r.json()


# ------------------------------------------------------------------ выход со всех устройств
async def test_logout_all_ends_every_session_of_this_user_only(api):
    email, phone = await _tokens(api, "multi")
    laptop = (await api.post(f"{USERS}/token", data={"username": email, "password": PASSWORD})).json()
    _, other = await _tokens(api, "someone-else")

    r = await api.post(f"{USERS}/me/logout-all", headers={"Authorization": f"Bearer {phone['access_token']}"})
    assert r.status_code == 204

    for tokens in (phone, laptop):
        assert (await api.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})).status_code == 401
    assert (await api.post(f"{USERS}/refresh", json={"refresh_token": other["refresh_token"]})).status_code == 200
    # войти заново можно
    assert (await api.post(f"{USERS}/token", data={"username": email, "password": PASSWORD})).status_code == 200


async def test_logout_all_needs_login(api):
    assert (await api.post(f"{USERS}/me/logout-all")).status_code == 401


# ------------------------------------------------------------------ чистка базы
async def _count(db, model):
    return (await db.execute(select(func.count()).select_from(model))).scalar()


async def test_cleanup_removes_only_stale_rows(db, org_a, monkeypatch):
    monkeypatch.setattr(settings, "activity_log_retention_days", 365)
    now = utc_now()
    user = models.User(username="cleanup-user", email="cleanup@example.com", password_hash="x")
    db.add(user)
    await db.flush()
    user_id = user.id

    def token(model, n, **kw):
        return model(user_id=user_id, token=f"{model.__tablename__[:6]}-{n}", **kw)

    db.add_all([
        # refresh: живой / погашенный только что / погашенный давно / истёкший
        token(models.RefreshToken, 1, expires_at=now + timedelta(days=10)),
        token(models.RefreshToken, 2, expires_at=now + timedelta(days=10), revoked=True),
        token(models.RefreshToken, 3, expires_at=now + timedelta(days=10), revoked=True,
              created_at=now - timedelta(days=2)),
        token(models.RefreshToken, 4, expires_at=now - timedelta(days=2)),
        # сброс пароля и подтверждение почты: живой / использованный давно / истёкший
        token(models.PasswordResetToken, 1, expires_at=now + timedelta(hours=1)),
        token(models.PasswordResetToken, 2, expires_at=now + timedelta(hours=1), used=True,
              created_at=now - timedelta(days=2)),
        token(models.PasswordResetToken, 3, expires_at=now - timedelta(days=2)),
        token(models.EmailVerificationToken, 1, expires_at=now + timedelta(hours=1)),
        token(models.EmailVerificationToken, 2, expires_at=now - timedelta(days=2)),
    ])
    db.add_all([
        models.Invitation(organization_id=org_a.org_id, email="new@example.com", role=models.MembershipRole.admin,
                          token="inv-fresh", expires_at=now - timedelta(days=5)),
        models.Invitation(organization_id=org_a.org_id, email="old@example.com", role=models.MembershipRole.admin,
                          token="inv-old", expires_at=now - timedelta(days=40), accepted=True),
    ])
    service = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org_a.org_id, full_name="Ann")
    client = models.Client(organization_id=org_a.org_id, full_name="C", phone="+380670009999")
    db.add_all([service, master, client])
    await db.flush()
    appointments = []
    for days_ago in (90, 10):
        start = datetime.now().replace(microsecond=0) - timedelta(days=days_ago)
        a = models.Appointment(organization_id=org_a.org_id, client_id=client.id, service_id=service.id,
                               master_id=master.id, start_time=start, end_time=start + timedelta(hours=1),
                               status="completed", price=1000, currency="EUR")
        db.add(a)
        await db.flush()
        appointments.append(a.id)
        db.add(models.AppointmentToken(appointment_id=a.id, token_hash=f"appt-{days_ago}"))
    db.add_all([
        models.AiChatMessage(organization_id=org_a.org_id, status="done", created_at=now - timedelta(days=500)),
        models.AiChatMessage(organization_id=org_a.org_id, status="done", created_at=now - timedelta(days=30)),
        models.ActivityLog(organization_id=org_a.org_id, action="create", entity_type="client", entity_id=1,
                           created_at=now - timedelta(days=400)),
        models.ActivityLog(organization_id=org_a.org_id, action="create", entity_type="client", entity_id=2,
                           created_at=now - timedelta(days=100)),
    ])
    await db.commit()
    logs_before = await _count(db, models.ActivityLog)

    removed = await purge_old_rows(db)

    assert removed["refresh_tokens"] == 2 and removed["password_reset_tokens"] == 2
    assert removed["email_verification_tokens"] == 1 and removed["invitations"] == 1
    assert removed["appointment_tokens"] == 1 and removed["ai_chat_messages"] == 1
    assert removed["activity_logs"] == 1
    mine = select(models.RefreshToken.token).where(models.RefreshToken.user_id == user_id)
    assert sorted((await db.execute(mine)).scalars()) == ["refres-1", "refres-2"]
    assert (await db.execute(select(models.Invitation.token))).scalars().all() == ["inv-fresh"]
    kept = (await db.execute(select(models.AppointmentToken.appointment_id))).scalars().all()
    assert kept == [appointments[1]]
    # сами записи не трогаются
    assert await _count(db, models.Appointment) == 2
    assert await _count(db, models.ActivityLog) == logs_before - 1

    # повторный запуск ничего не находит
    assert not any((await purge_old_rows(db)).values())


async def test_activity_log_kept_forever_when_retention_is_zero(db, org_a, monkeypatch):
    monkeypatch.setattr(settings, "activity_log_retention_days", 0)
    db.add(models.ActivityLog(organization_id=org_a.org_id, action="create", entity_type="client", entity_id=1,
                              created_at=utc_now() - timedelta(days=3000)))
    await db.commit()
    assert (await purge_old_rows(db))["activity_logs"] == 0
    assert await _count(db, models.ActivityLog) >= 1
