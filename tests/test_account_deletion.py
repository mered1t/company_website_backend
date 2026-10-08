from datetime import datetime

import pytest
from sqlalchemy import func, select

import models
from auth.auth import create_access_token, hash_password
from db.database import Base

PASSWORD = "Passw0rd!"  # пароль владельцев в тестовых организациях (см. conftest)
URL = "/api/v1/users/me/delete"
OWNER_ERROR = "Cannot delete account while you own an organization. Transfer ownership or delete the organization first."


async def _add_member(db, org, role, name=None):
    """Новый участник организации с настоящим токеном. Возвращает заголовки запроса и пользователя."""
    name = name or f"{role}-{org.org_id}"
    user = models.User(username=name, email=f"{name}@example.com", password_hash=hash_password(PASSWORD))
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org.org_id, role=models.MembershipRole(role)))
    await db.commit()
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}, user


async def _me(api, headers):
    r = await api.get("/api/v1/users/me", headers=headers)
    assert r.status_code == 200
    return r.json()


async def _count(db, model, *where):
    return (await db.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def _user_exists(db, user_id):
    return await _count(db, models.User, models.User.id == user_id) == 1


def _body(password=PASSWORD):
    return {"password": password}


# ------------------------------------------------------------------ проверки доступа

async def test_wrong_password_keeps_the_account(api, db, org_a):
    admin_headers, admin = await _add_member(db, org_a, "admin")
    r = await api.post(URL, headers=admin_headers, json=_body("Wrong-pass1"))
    assert r.status_code == 400
    assert r.json()["detail"] == "Incorrect password"
    assert await _user_exists(db, admin.id)
    assert await _count(db, models.Membership, models.Membership.user_id == admin.id) == 1


@pytest.mark.parametrize("body", [{}, {"password": ""}, {"password": "x" * 129}])
async def test_password_is_required_and_limited(api, db, org_a, body):
    admin_headers, admin = await _add_member(db, org_a, "admin")
    r = await api.post(URL, headers=admin_headers, json=body)
    assert r.status_code == 422
    assert await _user_exists(db, admin.id)


async def test_requires_login(api, db, org_a):
    r = await api.post(URL, json=_body())
    assert r.status_code == 401


async def test_old_endpoint_without_password_is_gone(api, db, org_a):
    """Раньше аккаунт удалялся одним токеном, без пароля. Этого пути больше нет."""
    admin_headers, admin = await _add_member(db, org_a, "admin")
    r = await api.delete(f"/api/v1/users/{admin.id}", headers=admin_headers)
    assert r.status_code in (404, 405)
    assert await _user_exists(db, admin.id)


# ------------------------------------------------------------------ владелец

async def test_owner_cannot_delete_while_owning_an_organization(api, db, org_a):
    me = await _me(api, org_a.headers)
    r = await api.post(URL, headers=org_a.headers, json=_body())
    assert r.status_code == 400
    assert r.json()["detail"] == OWNER_ERROR
    assert await _user_exists(db, me["id"])
    assert await _count(db, models.Organization, models.Organization.id == org_a.org_id) == 1
    # и после отказа аккаунт работает как раньше
    assert (await api.get("/api/v1/users/me", headers=org_a.headers)).status_code == 200


async def test_wrong_password_is_reported_before_the_owner_problem(api, db, org_a):
    r = await api.post(URL, headers=org_a.headers, json=_body("Wrong-pass1"))
    assert r.status_code == 400
    assert r.json()["detail"] == "Incorrect password"


async def test_owner_can_delete_after_transferring_ownership(api, db, org_a):
    me = await _me(api, org_a.headers)
    _, admin = await _add_member(db, org_a, "admin")
    r = await api.post(f"{org_a.base}/transfer-ownership", headers=org_a.headers,
                       json={"new_owner_user_id": admin.id, "password": PASSWORD})
    assert r.status_code == 204

    r = await api.post(URL, headers=org_a.headers, json=_body())
    assert r.status_code == 204
    assert not await _user_exists(db, me["id"])
    # организация осталась, у неё новый владелец
    owner_roles = (await db.execute(
        select(models.Membership.user_id).where(
            models.Membership.organization_id == org_a.org_id,
            models.Membership.role == models.MembershipRole.owner,
        ),
    )).scalars().all()
    assert owner_roles == [admin.id]


async def test_owner_can_delete_after_deleting_the_organization(api, db, org_a):
    me = await _me(api, org_a.headers)
    r = await api.post(f"{org_a.base}/delete", headers=org_a.headers,
                       json={"password": PASSWORD, "confirm_name": "Shop ownera"})
    assert r.status_code == 204
    r = await api.post(URL, headers=org_a.headers, json=_body())
    assert r.status_code == 204
    assert not await _user_exists(db, me["id"])


# ------------------------------------------------------------------ что удаляется

async def test_member_deletes_account_and_the_organization_stays(api, db, org_a, org_b):
    admin_headers, admin = await _add_member(db, org_a, "admin")
    r = await api.post(URL, headers=admin_headers, json=_body())
    assert r.status_code == 204

    assert not await _user_exists(db, admin.id)
    assert await _count(db, models.Membership, models.Membership.user_id == admin.id) == 0
    assert await _count(db, models.Organization, models.Organization.id == org_a.org_id) == 1
    assert await _count(db, models.Membership, models.Membership.organization_id == org_a.org_id) == 1  # владелец
    # чужие аккаунты и организации не затронуты
    assert (await api.get("/api/v1/users/me", headers=org_a.headers)).status_code == 200
    assert (await api.get("/api/v1/users/me", headers=org_b.headers)).status_code == 200


async def test_old_tokens_and_login_stop_working(api, db, org_a):
    admin_headers, admin = await _add_member(db, org_a, "admin")
    login = await api.post("/api/v1/users/token", data={"username": admin.email, "password": PASSWORD})
    assert login.status_code == 200
    refresh = login.json()["refresh_token"]

    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204

    assert (await api.get("/api/v1/users/me", headers=admin_headers)).status_code == 401
    assert (await api.post("/api/v1/users/token", data={"username": admin.email, "password": PASSWORD})).status_code == 401
    assert (await api.post("/api/v1/users/refresh", json={"refresh_token": refresh})).status_code == 401


async def test_personal_tokens_are_removed(api, db, org_a):
    me = await _me(api, org_a.headers)
    _, admin = await _add_member(db, org_a, "admin")
    admin_headers = {"Authorization": f"Bearer {create_access_token({'sub': str(admin.id)})}"}
    await api.post("/api/v1/users/token", data={"username": admin.email, "password": PASSWORD})
    db.add(models.PasswordResetToken(user_id=admin.id, token="r" * 40, expires_at=datetime(2999, 1, 1)))
    db.add(models.EmailVerificationToken(user_id=admin.id, token="v" * 40, expires_at=datetime(2999, 1, 1)))
    await db.commit()

    for model in (models.RefreshToken, models.PasswordResetToken, models.EmailVerificationToken):
        assert await _count(db, model, model.user_id == admin.id) >= 1

    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204

    for model in (models.RefreshToken, models.PasswordResetToken, models.EmailVerificationToken):
        assert await _count(db, model, model.user_id == admin.id) == 0
    assert await _user_exists(db, me["id"])


async def test_email_and_username_can_be_registered_again(api, db, org_a):
    admin_headers, admin = await _add_member(db, org_a, "admin")
    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204
    r = await api.post("/api/v1/users", json={
        "username": admin.username, "email": admin.email, "password": PASSWORD, "accept_terms": True,
    })
    assert r.status_code == 201


async def test_invitations_to_the_deleted_email_are_removed(api, db, org_a, org_b):
    admin_headers, admin = await _add_member(db, org_a, "admin")

    def invitation(org, email, token):
        return models.Invitation(organization_id=org.org_id, email=email, role=models.MembershipRole.master,
                                 token=token, expires_at=datetime(2999, 1, 1))

    db.add_all([
        invitation(org_b, f"Admin-{org_a.org_id}@Example.com", "t" * 40 + "1"),  # другая запись регистра
        invitation(org_a, "someone-else@example.com", "t" * 40 + "2"),
    ])
    await db.commit()

    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204

    left = (await db.execute(select(models.Invitation.email))).scalars().all()
    assert left == ["someone-else@example.com"]


async def test_activity_log_is_kept_but_personal_data_is_removed(api, db, org_a, org_b):
    owner = await _me(api, org_a.headers)
    admin_headers, admin = await _add_member(db, org_a, "admin")

    def log(org_id, user_id, details):
        return models.ActivityLog(organization_id=org_id, user_id=user_id, action="created",
                                  entity_type="x", entity_id=1, details=details)

    db.add_all([
        log(org_a.org_id, owner["id"], f"Invited {admin.email.upper()} as master"),   # чужая строка с его email
        log(org_b.org_id, owner["id"], f"Resent invitation to {admin.email}"),         # и в другой организации
        log(org_a.org_id, admin.id, f"{admin.username} accepted invitation as admin"),  # его собственная строка
        log(org_a.org_id, admin.id, "Created client Bob"),                              # его строка без личных данных
        log(org_a.org_id, owner["id"], "Created client Ivan"),                          # чужая строка без его данных
        log(org_a.org_id, owner["id"], f"{admin.username} is a nice name"),             # чужая строка: имя не трогаем
    ])
    await db.commit()
    total = await _count(db, models.ActivityLog)

    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204

    assert await _count(db, models.ActivityLog) == total  # журнал не теряем
    rows = (await db.execute(select(models.ActivityLog.user_id, models.ActivityLog.details)
                             .order_by(models.ActivityLog.id))).all()
    by_details = {d: u for u, d in rows}
    assert "Invited [deleted] as master" in by_details and by_details["Invited [deleted] as master"] == owner["id"]
    assert "Resent invitation to [deleted]" in by_details
    assert "[deleted] accepted invitation as admin" in by_details
    assert by_details["[deleted] accepted invitation as admin"] is None
    assert by_details["Created client Bob"] is None            # автор исчез, текст остался
    assert by_details["Created client Ivan"] == owner["id"]
    assert f"{admin.username} is a nice name" in by_details
    assert not any(admin.email.lower() in (d or "").lower() for _, d in rows)


async def test_email_with_sql_wildcards_does_not_redact_other_rows(api, db, org_a):
    """_ и % в email не должны работать как маски поиска."""
    owner = await _me(api, org_a.headers)
    admin_headers, admin = await _add_member(db, org_a, "admin", name="ad_min")
    db.add(models.ActivityLog(organization_id=org_a.org_id, user_id=owner["id"], action="x", entity_type="x",
                              entity_id=1, details="Invited adXmin@example.com as master"))
    await db.commit()

    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204

    details = (await db.execute(select(models.ActivityLog.details))).scalars().all()
    assert "Invited adXmin@example.com as master" in details


async def test_comments_stay_without_an_author(api, db, org_a):
    admin_headers, admin = await _add_member(db, org_a, "admin")
    client = models.Client(organization_id=org_a.org_id, full_name="Ivan", phone="+380501112233")
    db.add(client)
    await db.flush()
    db.add(models.ClientComment(client_id=client.id, user_id=admin.id, content="Любит короткую стрижку"))
    await db.commit()

    assert (await api.post(URL, headers=admin_headers, json=_body())).status_code == 204

    comments = (await db.execute(select(models.ClientComment.user_id, models.ClientComment.content))).all()
    assert comments == [(None, "Любит короткую стрижку")]


async def test_master_record_and_appointments_survive(api, db, org_a):
    """Карточка мастера принадлежит салону: она и её записи остаются, даже если мастер удалил свой аккаунт."""
    master = models.Master(organization_id=org_a.org_id, full_name="Petr")
    db.add(master)
    await db.flush()
    user = models.User(username="petr", email="petr@example.com", password_hash=hash_password(PASSWORD))
    db.add(user)
    await db.flush()
    db.add(models.Membership(user_id=user.id, organization_id=org_a.org_id, master_id=master.id,
                             role=models.MembershipRole.master))
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}

    assert (await api.post(URL, headers=headers, json=_body())).status_code == 204

    assert await _count(db, models.Master, models.Master.id == master.id) == 1
    assert not await _user_exists(db, user.id)


# ------------------------------------------------------------------ надёжность

async def test_delete_is_all_or_nothing(api, db, org_a, monkeypatch):
    """Если что-то сломалось после удаления части данных, аккаунт должен остаться целым."""
    import routers.users as users_router

    admin_headers, admin = await _add_member(db, org_a, "admin")
    db.add(models.ActivityLog(organization_id=org_a.org_id, user_id=admin.id, action="x", entity_type="x",
                              entity_id=1, details=f"Invited {admin.email}"))
    await db.commit()
    real_delete = users_router.delete_user_account

    async def broken(db_session, user):
        await real_delete(db_session, user)
        raise RuntimeError("something went wrong in the middle")

    monkeypatch.setattr(users_router, "delete_user_account", broken)
    with pytest.raises(RuntimeError):
        await api.post(URL, headers=admin_headers, json=_body())

    assert await _user_exists(db, admin.id)
    assert await _count(db, models.Membership, models.Membership.user_id == admin.id) == 1
    details = (await db.execute(select(models.ActivityLog.details))).scalars().all()
    assert f"Invited {admin.email}" in details  # правка журнала откатилась вместе с остальным
    assert not any("[deleted]" in (d or "") for d in details)


def test_every_foreign_key_to_users_is_handled():
    """Если упало: появилась таблица со ссылкой на users. Решите, что с ней делать при удалении аккаунта:
    ON DELETE SET NULL (автор становится пустым), ON DELETE CASCADE (строка уходит вместе с пользователем)
    или явное удаление в services/account.py (тогда добавьте таблицу в deleted_by_service ниже)."""
    deleted_by_service = {"memberships"}
    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name != "users":
                continue
            assert table.name in deleted_by_service or fk.ondelete in ("CASCADE", "SET NULL"), (
                f"{table.name}.{fk.parent.name} ссылается на users без ON DELETE: удаление аккаунта упадёт с ошибкой"
            )
