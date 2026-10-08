import models
from domain.plans import Plan, plan_includes_ai
from schemas.schemas import OrganizationPublic


async def test_new_organization_is_on_basic_plan(db, org_a):
    org = await db.get(models.Organization, org_a.org_id)
    assert org.plan == "basic"


def test_plan_helpers():
    assert plan_includes_ai(Plan.pro)
    assert plan_includes_ai("pro")
    assert not plan_includes_ai(Plan.basic)
    assert not plan_includes_ai("basic")
    assert not plan_includes_ai("something-else")


def test_organization_schema_exposes_plan():
    assert "plan" in OrganizationPublic.model_fields


async def test_my_organizations_shows_plan(api, db, org_a):
    r = await api.get("/api/v1/organizations", headers=org_a.headers)
    assert r.status_code == 200
    mine = [o for o in r.json() if o["id"] == org_a.org_id][0]
    assert mine["plan"] == "basic"

    org = await db.get(models.Organization, org_a.org_id)
    org.plan = "pro"
    await db.commit()

    r = await api.get("/api/v1/organizations", headers=org_a.headers)
    mine = [o for o in r.json() if o["id"] == org_a.org_id][0]
    assert mine["plan"] == "pro"
