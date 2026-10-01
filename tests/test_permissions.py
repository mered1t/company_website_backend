"""Матрица прав: кто из ролей что может.

Ничего не меняет в базе: ходим по несуществующим id (999999) и с пустым телом.
Для запрещённой роли ждём ровно 403, для разрешённой — что проверка прав пройдена
(любой другой ответ: 404, 422 и т.п.).
"""
import models
from tests.test_invitations import register_and_login

OWNER, ADMIN, MASTER = "owner", "admin", "master"
EVERYONE = {OWNER, ADMIN, MASTER}
MANAGERS = {OWNER, ADMIN}
OWNER_ONLY = {OWNER}

X = 999999

# (метод, путь внутри организации, кому разрешено)
MATRIX = [
    # --- записи ---
    ("GET", "/appointments", EVERYONE),
    ("GET", "/appointments/calendar", EVERYONE),
    ("POST", f"/appointments/{X}/restore", MANAGERS),
    ("DELETE", f"/appointments/{X}/permanent", OWNER_ONLY),
    # --- клиенты ---
    ("GET", "/clients", EVERYONE),
    ("GET", f"/clients/{X}", EVERYONE),
    ("GET", f"/clients/{X}/appointments", EVERYONE),
    ("GET", f"/clients/{X}/activity", EVERYONE),
    ("GET", f"/clients/{X}/comments", EVERYONE),
    ("DELETE", f"/clients/{X}/comments/{X}", MANAGERS),
    ("GET", "/clients/export", MANAGERS),
    ("POST", "/clients/import", MANAGERS),
    ("POST", f"/clients/{X}/restore", MANAGERS),
    ("DELETE", f"/clients/{X}", MANAGERS),
    ("DELETE", f"/clients/{X}/permanent", OWNER_ONLY),
    # --- мастера ---
    ("GET", "/masters", EVERYONE),
    ("GET", f"/masters/{X}", EVERYONE),
    ("GET", f"/masters/{X}/time-off", EVERYONE),
    ("GET", f"/masters/{X}/schedule-exceptions", EVERYONE),
    ("POST", "/masters", MANAGERS),
    ("PATCH", f"/masters/{X}", MANAGERS),
    ("PUT", f"/masters/{X}/working-hours", MANAGERS),
    ("POST", f"/masters/{X}/time-off", MANAGERS),
    ("DELETE", f"/masters/{X}/time-off/{X}", MANAGERS),
    ("POST", f"/masters/{X}/schedule-exceptions", MANAGERS),
    ("DELETE", f"/masters/{X}/schedule-exceptions/{X}", MANAGERS),
    ("POST", f"/masters/{X}/restore", MANAGERS),
    ("DELETE", f"/masters/{X}", MANAGERS),
    ("DELETE", f"/masters/{X}/permanent", OWNER_ONLY),
    # --- услуги ---
    ("GET", "/services", EVERYONE),
    ("GET", f"/services/{X}", EVERYONE),
    ("POST", "/services", MANAGERS),
    ("PATCH", f"/services/{X}", MANAGERS),
    ("POST", f"/services/{X}/restore", MANAGERS),
    ("DELETE", f"/services/{X}", MANAGERS),
    ("DELETE", f"/services/{X}/permanent", OWNER_ONLY),
    # --- аналитика ---
    ("GET", "/analytics/revenue", MANAGERS),
    ("GET", "/analytics/top-clients", MANAGERS),
    ("GET", "/analytics/inactive-clients", MANAGERS),
    ("GET", "/analytics/popular-services", MANAGERS),
    ("GET", "/analytics/masters-workload", MANAGERS),
    ("GET", "/analytics/birthdays", MANAGERS),
    # --- организация, команда, приглашения ---
    ("PATCH", "", MANAGERS),
    ("GET", "/members", EVERYONE),
    ("GET", "/activity", MANAGERS),
    ("DELETE", f"/members/{X}", MANAGERS),
    ("GET", "/invitations", MANAGERS),
    ("POST", "/invitations", MANAGERS),
    ("POST", f"/invitations/{X}/resend", MANAGERS),
    ("DELETE", f"/invitations/{X}", MANAGERS),
]


async def _team(api, db, org_a):
    """Владелец + админ + мастер в одной организации."""
    headers = {OWNER: org_a.headers}
    for role in (models.MembershipRole.admin, models.MembershipRole.master):
        h, user_id = await register_and_login(api, f"{role.value}user", f"{role.value}team@example.com")
        db.add(models.Membership(user_id=user_id, organization_id=org_a.org_id, role=role))
        headers[role.value] = h
    await db.commit()
    return headers


async def test_permission_matrix(api, db, org_a):
    headers = await _team(api, db, org_a)

    mismatches = []
    for role in (OWNER, ADMIN, MASTER):
        for method, path, allowed in MATRIX:
            body = {} if method in ("POST", "PUT", "PATCH") else None
            r = await api.request(method, f"{org_a.base}{path}", headers=headers[role], json=body)

            if role in allowed:
                ok = r.status_code not in (401, 403)
                expected = "доступ разрешён"
            else:
                ok = r.status_code == 403
                expected = "ждали 403"
            if not ok:
                mismatches.append(f"{role:6} {method:6} {path or '/'}  → {expected}, получили {r.status_code}")

    assert not mismatches, "\n" + "\n".join(mismatches)