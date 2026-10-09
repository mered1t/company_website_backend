from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import models
from auth.auth import create_access_token, hash_password
from db.database import Base
from core.time_utils import utc_now

PASSWORD = "Passw0rd!"  # пароль владельцев в тестовых организациях (см. conftest)

# Все таблицы проекта делятся на данные салона (выгружаем и удаляем вместе с организацией) и общие.
ORG_TABLES = {
    "organizations", "memberships", "invitations", "services", "masters", "master_services", "working_hours",
    "time_off", "working_hours_exceptions", "clients", "client_comments", "appointments", "appointment_tokens",
    "activity_logs", "ai_reports", "notifications",
}
GLOBAL_TABLES = {"users", "password_reset_tokens", "refresh_tokens", "email_verification_tokens", "payments"}


def test_every_table_is_classified():
    """Если упало: в проекте появилась новая таблица. Решите, это данные салона или общая таблица.
    Данные салона нужно добавить в services/org_data.py (выгрузка и удаление) и в ORG_TABLES, общую в GLOBAL_TABLES."""
    assert set(Base.metadata.tables) == ORG_TABLES | GLOBAL_TABLES


async def _populate(db, org, tag):
    """Заполняет все таблицы салона. tag есть во всех текстах: по нему ищем утечки между организациями."""
    owner_id = (await db.execute(
        select(models.Membership.user_id).where(
            models.Membership.organization_id == org.org_id, models.Membership.role == models.MembershipRole.owner,
        ),
    )).scalar_one()
    service = models.Service(organization_id=org.org_id, name=f"{tag}-service", price=1500, duration_minutes=45,
                             description=f"{tag}-description")
    master = models.Master(organization_id=org.org_id, full_name=f"{tag}-master", phone="+380500000111")
    client = models.Client(organization_id=org.org_id, full_name=f"{tag}-client", phone="+380671111111",
                           email=f"{tag}-client@example.com", notes=f"{tag}-client-note")
    db.add_all([service, master, client])
    await db.flush()

    start = datetime(2030, 3, 1, 10, 0)
    appointment = models.Appointment(
        organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
        start_time=start, end_time=start + timedelta(minutes=45), status="completed", price=1500, currency="EUR",
        notes=f"{tag}-appointment-note",
    )
    db.add(appointment)
    await db.flush()
    db.add_all([
        models.MasterService(master_id=master.id, service_id=service.id),
        models.WorkingHours(master_id=master.id, day_of_week=1, start_time="09:00", end_time="18:00"),
        models.TimeOff(master_id=master.id, start_date=datetime(2030, 1, 1), end_date=datetime(2030, 1, 5),
                       reason=f"{tag}-vacation"),
        models.WorkingHoursException(master_id=master.id, date=datetime(2030, 2, 1), start_time="10:00", end_time="14:00"),
        models.AppointmentToken(appointment_id=appointment.id, token_hash=f"{tag}-token-hash", language="en",
                                email=f"{tag}-booking@example.com"),
        models.ClientComment(client_id=client.id, user_id=owner_id, content=f"{tag}-comment"),
        models.Invitation(organization_id=org.org_id, email=f"{tag}-invite@example.com",
                          role=models.MembershipRole.admin, token=f"{tag}-invite-token",
                          expires_at=utc_now() + timedelta(days=7)),
        models.ActivityLog(organization_id=org.org_id, user_id=owner_id, action="created", entity_type="client",
                           entity_id=client.id, details=f"{tag}-activity"),
        models.AiReport(organization_id=org.org_id, user_id=owner_id, period_start=datetime(2030, 1, 1),
                        period_end=datetime(2030, 1, 31), language="en", status="done", content=f"{tag}-report"),
        models.Notification(organization_id=org.org_id, user_id=owner_id, type="booking_created",
                            appointment_id=appointment.id, client_id=client.id, service_name=f"{tag}-service",
                            master_name=f"{tag}-master", start_time=start),
    ])
    await db.commit()
    return SimpleNamespace(owner_id=owner_id, service_id=service.id, master_id=master.id, client_id=client.id,
                           appointment_id=appointment.id)


async def _add_member(db, org, role):
    """Новый участник организации с настоящим токеном. Возвращает заголовки запроса и id пользователя."""
    user = models.User(username=f"{role}-{org.org_id}", email=f"{role}-{org.org_id}@example.com",
                       password_hash=hash_password(PASSWORD))
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole(role)))
    await db.commit()
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}, user.id


async def _counts(db, org_id):
    """Сколько строк данных салона осталось в каждой таблице."""
    appointment_ids = select(models.Appointment.id).where(models.Appointment.organization_id == org_id)
    client_ids = select(models.Client.id).where(models.Client.organization_id == org_id)
    master_ids = select(models.Master.id).where(models.Master.organization_id == org_id)
    queries = {
        "organizations": select(func.count()).select_from(models.Organization).where(models.Organization.id == org_id),
        "memberships": select(func.count()).select_from(models.Membership).where(models.Membership.organization_id == org_id),
        "invitations": select(func.count()).select_from(models.Invitation).where(models.Invitation.organization_id == org_id),
        "services": select(func.count()).select_from(models.Service).where(models.Service.organization_id == org_id),
        "masters": select(func.count()).select_from(models.Master).where(models.Master.organization_id == org_id),
        "clients": select(func.count()).select_from(models.Client).where(models.Client.organization_id == org_id),
        "appointments": select(func.count()).select_from(models.Appointment).where(models.Appointment.organization_id == org_id),
        "activity_logs": select(func.count()).select_from(models.ActivityLog).where(models.ActivityLog.organization_id == org_id),
        "ai_reports": select(func.count()).select_from(models.AiReport).where(models.AiReport.organization_id == org_id),
        "notifications": select(func.count()).select_from(models.Notification).where(models.Notification.organization_id == org_id),
        "appointment_tokens": select(func.count()).select_from(models.AppointmentToken).where(models.AppointmentToken.appointment_id.in_(appointment_ids)),
        "client_comments": select(func.count()).select_from(models.ClientComment).where(models.ClientComment.client_id.in_(client_ids)),
        "master_services": select(func.count()).select_from(models.MasterService).where(models.MasterService.master_id.in_(master_ids)),
        "working_hours": select(func.count()).select_from(models.WorkingHours).where(models.WorkingHours.master_id.in_(master_ids)),
        "time_off": select(func.count()).select_from(models.TimeOff).where(models.TimeOff.master_id.in_(master_ids)),
        "working_hours_exceptions": select(func.count()).select_from(models.WorkingHoursException).where(models.WorkingHoursException.master_id.in_(master_ids)),
    }
    assert set(queries) == ORG_TABLES  # счётчик покрывает все таблицы салона
    return {name: (await db.execute(query)).scalar_one() for name, query in queries.items()}


def _delete_body(name="Shop ownera", password=PASSWORD):
    return {"password": password, "confirm_name": name}


# ------------------------------------------------------------------ выгрузка

async def test_owner_exports_everything(api, db, org_a):
    ids = await _populate(db, org_a, "A")
    _, admin_id = await _add_member(db, org_a, "admin")
    await _add_member(db, org_a, "master")

    r = await api.get(f"{org_a.base}/export", headers=org_a.headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].startswith('attachment; filename="')
    assert r.headers["content-disposition"].endswith('.json"')
    data = r.json()

    assert data["format_version"] == 1
    assert data["organization"]["name"] == "Shop ownera"
    assert data["organization"]["timezone"] == "Europe/Kiev"
    assert sorted(m["role"] for m in data["members"]) == ["admin", "master", "owner"]

    assert [s["name"] for s in data["services"]] == ["A-service"]
    assert data["services"][0]["price"] == 1500

    (master,) = data["masters"]
    assert master["full_name"] == "A-master"
    assert master["service_ids"] == [ids.service_id]
    assert master["working_hours"] == [{"day_of_week": 1, "start_time": "09:00", "end_time": "18:00"}]
    assert master["time_off"][0]["reason"] == "A-vacation"
    assert master["working_hours_exceptions"][0]["start_time"] == "10:00"

    (client,) = data["clients"]
    assert client["full_name"] == "A-client" and client["email"] == "A-client@example.com"
    assert client["notes"] == "A-client-note"
    assert client["comments"][0]["content"] == "A-comment"
    owner = await db.get(models.User, ids.owner_id)
    assert client["comments"][0]["author_username"] == owner.username

    (appointment,) = data["appointments"]
    assert appointment["id"] == ids.appointment_id
    assert appointment["client_id"] == ids.client_id
    assert appointment["status"] == "completed" and appointment["price"] == 1500
    assert appointment["start_time"].startswith("2030-03-01T10:00")
    assert appointment["notes"] == "A-appointment-note"


async def test_export_has_only_own_data(api, db, org_a, org_b):
    await _populate(db, org_a, "A")
    await _populate(db, org_b, "B")

    r = await api.get(f"{org_a.base}/export", headers=org_a.headers)
    assert r.status_code == 200
    assert "A-client" in r.text
    assert "B-" not in r.text and "ownerb" not in r.text and "Shop ownerb" not in r.text


async def test_export_has_no_passwords_or_tokens(api, db, org_a):
    await _populate(db, org_a, "A")
    r = await api.get(f"{org_a.base}/export", headers=org_a.headers)
    assert r.status_code == 200
    for secret in ("argon2", "password", "A-token-hash", "A-invite-token", "token_hash"):
        assert secret not in r.text, secret


async def test_export_keeps_deleted_and_anonymized_clients_as_they_are(api, db, org_a):
    ids = await _populate(db, org_a, "A")
    client = await db.get(models.Client, ids.client_id)
    client.full_name, client.phone, client.email, client.notes = "Deleted client", "anon-1", None, None
    client.deleted_at = client.anonymized_at = utc_now()
    await db.commit()

    r = await api.get(f"{org_a.base}/export", headers=org_a.headers)
    (exported,) = r.json()["clients"]
    assert exported["full_name"] == "Deleted client"
    assert exported["anonymized_at"] is not None and exported["deleted_at"] is not None
    assert "A-client" not in r.text  # стёртые данные не возвращаются из истории


async def test_export_is_written_to_the_activity_log(api, db, org_a):
    await api.get(f"{org_a.base}/export", headers=org_a.headers)
    entry = (await db.execute(
        select(models.ActivityLog).where(models.ActivityLog.organization_id == org_a.org_id,
                                         models.ActivityLog.action == "exported"),
    )).scalars().one()
    assert entry.entity_type == "organization"


async def test_only_the_owner_can_export(api, db, org_a, org_b):
    admin_headers, _ = await _add_member(db, org_a, "admin")
    master_headers, _ = await _add_member(db, org_a, "master")

    assert (await api.get(f"{org_a.base}/export", headers=admin_headers)).status_code == 403
    assert (await api.get(f"{org_a.base}/export", headers=master_headers)).status_code == 403
    assert (await api.get(f"{org_a.base}/export", headers=org_b.headers)).status_code == 403  # владелец чужого салона
    assert (await api.get(f"{org_a.base}/export")).status_code == 401


# ------------------------------------------------------------------ удаление

async def test_delete_removes_everything_of_the_organization(api, db, org_a, org_b):
    await _populate(db, org_a, "A")
    await _populate(db, org_b, "B")
    await _add_member(db, org_a, "admin")
    assert all(count >= 1 for count in (await _counts(db, org_a.org_id)).values())
    b_before = await _counts(db, org_b.org_id)

    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body())
    assert r.status_code == 204, r.text

    assert all(count == 0 for count in (await _counts(db, org_a.org_id)).values())
    assert await _counts(db, org_b.org_id) == b_before  # чужая организация не тронута


async def test_delete_keeps_user_accounts_and_other_memberships(api, db, org_a, org_b):
    ids = await _populate(db, org_a, "A")
    db.add(models.Membership(user_id=ids.owner_id, organization_id=org_b.org_id, role=models.MembershipRole.admin))
    await db.commit()

    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body())
    assert r.status_code == 204

    assert await db.get(models.User, ids.owner_id) is not None
    remaining = (await db.execute(
        select(models.Membership.organization_id).where(models.Membership.user_id == ids.owner_id),
    )).scalars().all()
    assert remaining == [org_b.org_id]

    mine = await api.get("/api/v1/organizations", headers=org_a.headers)
    assert mine.status_code == 200
    assert [o["id"] for o in mine.json()] == [org_b.org_id]


async def test_deleted_organization_disappears_from_the_public_site(api, db, org_a):
    await _populate(db, org_a, "A")
    before = await api.get(f"/api/v1/public/{org_a.slug}/services")
    assert before.status_code == 200

    await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body())
    assert (await api.get(f"/api/v1/public/{org_a.slug}/services")).status_code == 404


async def test_delete_needs_the_right_password(api, db, org_a):
    await _populate(db, org_a, "A")
    before = await _counts(db, org_a.org_id)

    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body(password="wrong-password"))
    assert r.status_code == 400
    assert r.json()["detail"] == "Incorrect password"
    assert await _counts(db, org_a.org_id) == before


@pytest.mark.parametrize("name", ["", "Shop", "shop ownera", "Shop ownerb"])
async def test_delete_needs_the_exact_organization_name(api, db, org_a, name):
    await _populate(db, org_a, "A")
    before = await _counts(db, org_a.org_id)

    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body(name=name))
    assert r.status_code == 400
    assert r.json()["detail"] == "Organization name does not match"
    assert await _counts(db, org_a.org_id) == before


async def test_delete_ignores_spaces_around_the_name(api, db, org_a):
    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body(name="  Shop ownera "))
    assert r.status_code == 204


async def test_only_the_owner_can_delete(api, db, org_a, org_b):
    await _populate(db, org_a, "A")
    admin_headers, _ = await _add_member(db, org_a, "admin")
    master_headers, _ = await _add_member(db, org_a, "master")
    before = await _counts(db, org_a.org_id)
    url = f"{org_a.base}/delete"

    assert (await api.post(url, headers=admin_headers, json=_delete_body())).status_code == 403
    assert (await api.post(url, headers=master_headers, json=_delete_body())).status_code == 403
    assert (await api.post(url, headers=org_b.headers, json=_delete_body())).status_code == 403
    assert (await api.post(url, json=_delete_body())).status_code == 401
    assert await _counts(db, org_a.org_id) == before


async def test_second_delete_is_rejected_cleanly(api, db, org_a):
    assert (await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body())).status_code == 204
    again = await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body())
    assert again.status_code in (403, 404)


async def test_delete_is_all_or_nothing(api, db, org_a, monkeypatch):
    """Если что-то сломалось после удаления части данных, ничего не должно пропасть."""
    import routers.organizations as organizations_router

    await _populate(db, org_a, "A")
    before = await _counts(db, org_a.org_id)
    real_delete = organizations_router.delete_organization_data

    async def broken(db_session, organization_id):
        await real_delete(db_session, organization_id)
        raise RuntimeError("something went wrong in the middle")

    monkeypatch.setattr(organizations_router, "delete_organization_data", broken)
    with pytest.raises(RuntimeError):
        await api.post(f"{org_a.base}/delete", headers=org_a.headers, json=_delete_body())

    assert await _counts(db, org_a.org_id) == before
