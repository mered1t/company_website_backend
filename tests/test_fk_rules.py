from sqlalchemy import select

import models
from tests.test_invitations import make_invitation, register_and_login, verify_in_db
from tests.test_tenant_isolation import make_client, make_master, make_service


async def test_delete_user_keeps_activity_and_comments(api, db, org_a):
    ivan_headers, ivan_id = await register_and_login(api, "ivan", "ivan@example.com")
    await verify_in_db(db, "ivan@example.com")
    inv_id = await make_invitation(db, org_a.org_id, "ivan@example.com")
    r = await api.post(f"/api/invitations/{inv_id}/accept", headers=ivan_headers)
    assert r.status_code == 204, r.text

    # Иван что-то делает: клиент (пишет в журнал) и комментарий
    r = await api.post(f"{org_a.base}/clients", headers=ivan_headers,
                       json={"full_name": "Client", "phone": "+380990000010"})
    assert r.status_code == 201, r.text
    client_id = r.json()["id"]
    r = await api.post(f"{org_a.base}/clients/{client_id}/comments", headers=ivan_headers,
                       json={"content": "Notes from Ivan"})
    assert r.status_code == 201, r.text

    # Иван удаляет аккаунт
    r = await api.delete(f"/api/users/{ivan_id}", headers=ivan_headers)
    assert r.status_code == 204, r.text

    # журнал остался, автор обнулился
    logs = (await db.execute(
        select(models.ActivityLog).where(models.ActivityLog.organization_id == org_a.org_id),
    )).scalars().all()
    assert any(log.user_id is None for log in logs)

    # комментарий остался у клиента
    r = await api.get(f"{org_a.base}/clients/{client_id}/comments", headers=org_a.headers)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1


async def test_hard_delete_client_with_comments(api, org_a):
    client_id = await make_client(api, org_a, "+380990000011")
    r = await api.post(f"{org_a.base}/clients/{client_id}/comments", headers=org_a.headers,
                       json={"content": "hello"})
    assert r.status_code == 201, r.text

    assert (await api.delete(f"{org_a.base}/clients/{client_id}", headers=org_a.headers)).status_code == 204
    r = await api.delete(f"{org_a.base}/clients/{client_id}/permanent", headers=org_a.headers)
    assert r.status_code == 204, r.text


async def test_hard_delete_service_linked_to_master(api, org_a):
    service_id = await make_service(api, org_a)
    await make_master(api, org_a, service_ids=[service_id])

    assert (await api.delete(f"{org_a.base}/services/{service_id}", headers=org_a.headers)).status_code == 204
    r = await api.delete(f"{org_a.base}/services/{service_id}/permanent", headers=org_a.headers)
    assert r.status_code == 204, r.text