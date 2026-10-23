"""Фильтры списка клиентов: визиты, последний визит, неявки, email, день рождения, дата добавления.
Все работают вместе (И), вместе с q и status_id; X-Total-Count учитывает фильтр."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

import models
from services.common import get_org_now


@pytest.fixture
async def base(db, org_a):
    """Anna: 3 визита (последний 90 дней назад), Bob: 1 визит вчера (в чужой записи) и 2 неявки,
    Carl: визитов нет, email есть, день рождения в марте; Dan: удалён (в фильтры не попадает)."""
    now = await get_org_now(db, org_a.org_id)
    cut = models.Service(organization_id=org_a.org_id, name="Cut", price=1000, duration_minutes=30, max_party_size=2)
    ann = models.Master(organization_id=org_a.org_id, full_name="Ann")
    anna = models.Client(organization_id=org_a.org_id, full_name="Anna", phone="+380670001601",
                         email="anna@example.com", birth_date=datetime(1990, 7, 5),
                         created_at=datetime(2026, 1, 10, 12, 0))
    bob = models.Client(organization_id=org_a.org_id, full_name="Bob", phone="+380670001602",
                        created_at=datetime(2026, 2, 10, 12, 0))
    carl = models.Client(organization_id=org_a.org_id, full_name="Carl", phone="+380670001603",
                         email="carl@example.com", birth_date=datetime(1985, 3, 20),
                         created_at=datetime(2026, 3, 10, 12, 0))
    dan = models.Client(organization_id=org_a.org_id, full_name="Dan", phone="+380670001604",
                        deleted_at=datetime(2026, 3, 1))
    db.add_all([cut, ann, anna, bob, carl, dan])
    await db.flush()

    def visit(client, days_ago, status="completed", participants=()):
        start = (now - timedelta(days=days_ago)).replace(hour=10, minute=0, second=0, microsecond=0)
        return models.Appointment(
            organization_id=org_a.org_id, client_id=client.id, service_id=cut.id, master_id=ann.id,
            start_time=start, end_time=start + timedelta(minutes=30), status=status, price=1000, currency="EUR",
            participants=[models.AppointmentParticipant(organization_id=org_a.org_id, client_id=p.id, price=1000)
                          for p in participants])

    db.add_all([
        visit(anna, 200), visit(anna, 150), visit(anna, 90), visit(anna, 30, status="no_show"),
        visit(bob, 20, status="no_show"), visit(bob, 10, status="no_show"),
        visit(carl, 1, status="cancelled"),
        visit(dan, 1, participants=[bob]),  # Bob был вчера в чужой записи: визит у него есть
    ])
    await db.commit()
    return SimpleNamespace(now=now)


async def _names(api, org, **params):
    r = await api.get(f"{org.base}/clients", headers=org.headers, params=params)
    assert r.status_code == 200, r.text
    names = [c["full_name"] for c in r.json()]
    assert r.headers["x-total-count"] == str(len(names))
    return names


async def test_visit_filters(api, org_a, base):
    assert await _names(api, org_a, visits_min=3) == ["Anna"]
    assert await _names(api, org_a, visits_min=1, visits_max=2) == ["Bob"]  # визит в чужой записи тоже считается
    assert await _names(api, org_a, never_visited="true") == ["Carl"]  # отменённая запись — не визит
    assert await _names(api, org_a, never_visited="false") == ["Anna", "Bob"]
    assert await _names(api, org_a, no_shows_min=2) == ["Bob"]
    assert await _names(api, org_a, no_shows_min=1) == ["Anna", "Bob"]


async def test_last_visit_filters(api, org_a, base):
    sixty_days_ago = (base.now - timedelta(days=60)).date().isoformat()
    assert await _names(api, org_a, last_visit_before=sixty_days_ago) == ["Anna"]  # без визитов не входят
    assert await _names(api, org_a, last_visit_after=sixty_days_ago) == ["Bob"]
    yesterday = (base.now - timedelta(days=1)).date().isoformat()
    assert await _names(api, org_a, last_visit_after=yesterday) == ["Bob"]  # в эту дату — входит
    # «постоянные, которые не были больше 60 дней»
    assert await _names(api, org_a, visits_min=3, last_visit_before=sixty_days_ago) == ["Anna"]
    assert await _names(api, org_a, visits_min=3, last_visit_after=sixty_days_ago) == []


async def test_email_birthday_and_created(api, org_a, base):
    assert await _names(api, org_a, has_email="true") == ["Anna", "Carl"]
    assert await _names(api, org_a, has_email="false") == ["Bob"]
    assert await _names(api, org_a, birthday_month=3) == ["Carl"]
    assert await _names(api, org_a, birthday_month=3, has_email="true", never_visited="true") == ["Carl"]
    assert await _names(api, org_a, created_after="2026-02-10") == ["Bob", "Carl"]  # включительно
    assert await _names(api, org_a, created_before="2026-02-10") == ["Anna", "Bob"]
    assert await _names(api, org_a, created_after="2026-02-01", created_before="2026-02-28") == ["Bob"]


async def test_filters_combine_with_search_status_and_paging(api, org_a, base):
    statuses = (await api.get(f"{org_a.base}/client-statuses", headers=org_a.headers)).json()
    clients = {c["full_name"]: c["id"] for c in
               (await api.get(f"{org_a.base}/clients", headers=org_a.headers)).json()}
    await api.patch(f"{org_a.base}/clients/{clients['Anna']}", headers=org_a.headers,
                    json={"status_id": statuses[1]["id"]})
    assert await _names(api, org_a, has_email="true", status_id=statuses[1]["id"]) == ["Anna"]
    assert await _names(api, org_a, has_email="true", q="car") == ["Carl"]
    r = await api.get(f"{org_a.base}/clients", headers=org_a.headers,
                      params={"never_visited": "false", "sort": "visits", "dir": "desc", "limit": 1})
    assert [c["full_name"] for c in r.json()] == ["Anna"] and r.headers["x-total-count"] == "2"


@pytest.mark.parametrize("params", [
    {"visits_min": -1}, {"visits_min": "x"}, {"birthday_month": 13}, {"birthday_month": 0},
    {"last_visit_before": "2026-13-01"}, {"created_after": "yesterday"}, {"has_email": "maybe"},
    {"visits_min": 5, "visits_max": 2}, {"created_after": "2026-03-01", "created_before": "2026-02-01"},
    {"last_visit_after": "2026-03-01", "last_visit_before": "2026-03-01"},
])
async def test_bad_values_are_422(api, org_a, params):
    r = await api.get(f"{org_a.base}/clients", headers=org_a.headers, params=params)
    assert r.status_code == 422, r.text
