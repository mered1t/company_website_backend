from datetime import datetime

import models


def _dt(day, hour=0):
    return datetime(2030, 1, day, hour)


async def _seed(db, org):
    client_a = models.Client(organization_id=org.org_id, full_name="Anna", phone="+10000001")
    client_b = models.Client(organization_id=org.org_id, full_name="Boris", phone="+10000002")
    service = models.Service(organization_id=org.org_id, name="Haircut", price=1000, duration_minutes=60)
    master = models.Master(organization_id=org.org_id, full_name="Master One")
    db.add_all([client_a, client_b, service, master])
    await db.flush()

    def appt(client, day, hour, price, status="completed"):
        return models.Appointment(
            organization_id=org.org_id, client_id=client.id, service_id=service.id, master_id=master.id,
            start_time=_dt(day, hour), end_time=_dt(day, hour + 1),
            status=status, price=price, currency="EUR",
        )

    db.add_all([
        appt(client_a, 7, 10, 1000),
        appt(client_a, 14, 10, 1000),
        appt(client_b, 14, 11, 2000),
        appt(client_b, 15, 10, 500, status="cancelled"),
    ])
    await db.commit()


WEEK = {"date_from": "2030-01-14T00:00:00", "date_to": "2030-01-20T23:59:59"}
TWO_WEEKS = {"date_from": "2030-01-07T00:00:00", "date_to": "2030-01-20T23:59:59"}


async def test_revenue_has_currency(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/revenue", params=WEEK, headers=org_a.headers)
    assert r.status_code == 200
    assert r.json()["total_revenue"] == 3000
    assert r.json()["currency"] == "EUR"


async def test_revenue_trend_by_day_with_previous_period(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/revenue-trend", params={**WEEK, "group_by": "day"}, headers=org_a.headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total_revenue"] == 3000
    assert data["previous_total_revenue"] == 1000
    assert data["change_percent"] == 200.0
    assert len(data["points"]) == 7  # дни без записей тоже есть, с нулями
    assert data["points"][0]["revenue"] == 3000
    assert data["points"][0]["appointments"] == 2
    assert data["points"][1]["revenue"] == 0


async def test_revenue_trend_by_week(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/revenue-trend", params={**WEEK, "group_by": "week"}, headers=org_a.headers)
    assert r.status_code == 200
    points = r.json()["points"]
    assert len(points) == 1
    assert points[0]["revenue"] == 3000


async def test_revenue_trend_by_month(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/revenue-trend", params={**TWO_WEEKS, "group_by": "month"}, headers=org_a.headers)
    assert r.status_code == 200
    points = r.json()["points"]
    assert len(points) == 1
    assert points[0]["revenue"] == 4000


async def test_period_validation(api, org_a):
    url = f"{org_a.base}/analytics/revenue-trend"
    r = await api.get(url, params={"date_from": "2030-01-20T00:00:00", "date_to": "2030-01-10T00:00:00"}, headers=org_a.headers)
    assert r.status_code == 422
    r = await api.get(url, params={"date_from": "2020-01-01T00:00:00", "date_to": "2030-01-01T00:00:00"}, headers=org_a.headers)
    assert r.status_code == 422
    r = await api.get(url, params={**WEEK, "group_by": "year"}, headers=org_a.headers)
    assert r.status_code == 422


async def test_limits_are_validated(api, org_a):
    for path, params in [
        ("top-clients", {"limit": 0}),
        ("top-clients", {"limit": 101}),
        ("popular-services", {"limit": -1}),
        ("inactive-clients", {"days": 0}),
        ("inactive-clients", {"limit": 0}),
    ]:
        r = await api.get(f"{org_a.base}/analytics/{path}", params=params, headers=org_a.headers)
        assert r.status_code == 422, (path, params)


async def test_top_clients_and_services_accept_period(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/top-clients", params=WEEK, headers=org_a.headers)
    assert r.status_code == 200
    assert [c["full_name"] for c in r.json()] == ["Boris", "Anna"]
    r = await api.get(f"{org_a.base}/analytics/popular-services", params=WEEK, headers=org_a.headers)
    assert r.json()[0]["times_booked"] == 2
    # только одна граница периода -- ошибка
    r = await api.get(f"{org_a.base}/analytics/top-clients", params={"date_from": "2030-01-14T00:00:00"}, headers=org_a.headers)
    assert r.status_code == 422


async def test_appointments_summary(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/appointments-summary", params=TWO_WEEKS, headers=org_a.headers)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 4
    assert data["completed"] == 3
    assert data["cancelled"] == 1
    assert data["cancellation_rate_percent"] == 25.0
    assert data["average_check"] == 1333  # 4000 / 3
    assert data["currency"] == "EUR"


async def test_clients_summary_new_vs_returning(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/clients-summary", params=WEEK, headers=org_a.headers)
    assert r.status_code == 200
    data = r.json()
    assert data["new_clients"] == 1  # Boris: первый визит 14 января
    assert data["returning_clients"] == 1  # Anna: была 7 января
    assert data["returning_share_percent"] == 50.0


async def test_busiest_hours_skips_cancelled(api, db, org_a):
    await _seed(db, org_a)
    r = await api.get(f"{org_a.base}/analytics/busiest-hours", params=TWO_WEEKS, headers=org_a.headers)
    assert r.status_code == 200
    # 7 и 14 января 2030 -- понедельники; отменённая запись 15 января не считается
    assert r.json() == [
        {"weekday": 0, "hour": 10, "appointments": 2},
        {"weekday": 0, "hour": 11, "appointments": 1},
    ]


async def test_analytics_do_not_leak_between_organizations(api, db, org_a, org_b):
    await _seed(db, org_a)
    for path in ("revenue", "appointments-summary", "clients-summary"):
        r = await api.get(f"{org_b.base}/analytics/{path}", params=TWO_WEEKS, headers=org_b.headers)
        assert r.status_code == 200
        assert not any(v for k, v in r.json().items() if k in ("total_revenue", "total", "new_clients", "returning_clients"))
    r = await api.get(f"{org_b.base}/analytics/busiest-hours", params=TWO_WEEKS, headers=org_b.headers)
    assert r.json() == []
