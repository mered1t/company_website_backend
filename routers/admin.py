"""Админка платформы: подписки организаций. Доступна только пользователям с флагом is_platform_admin.

Флаг выставляется скриптом scripts/make_admin.py. Пользоваться можно прямо из Swagger (/docs) под своим токеном.
"""
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import PlatformAdmin
from db.database import get_db
from domain.subscription import SubscriptionStatus, get_subscription
from schemas.billing import (
    AdminOrganizationDetail,
    AdminOrganizationPublic,
    AdminOrganizationUpdate,
    PaymentCreate,
    PaymentPublic,
    PaymentResult,
    SubscriptionInfo,
)
from services.billing import record_payment

router = APIRouter()


async def _owner_emails(db: AsyncSession, org_ids: list[int]) -> dict[int, str]:
    if not org_ids:
        return {}
    rows = (await db.execute(
        select(models.Membership.organization_id, models.User.email)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(
            models.Membership.organization_id.in_(org_ids),
            models.Membership.role == models.MembershipRole.owner,
        ),
    )).all()
    return {org_id: email for org_id, email in rows}


def _public(org: models.Organization, owner_email: str | None) -> AdminOrganizationPublic:
    return AdminOrganizationPublic(
        id=org.id,
        name=org.name,
        slug=org.slug,
        created_at=org.created_at,
        owner_email=owner_email,
        plan=org.plan,
        is_free=org.is_free,
        is_blocked=org.is_blocked,
        billing_note=org.billing_note,
        subscription=SubscriptionInfo.from_org(org),
    )


async def _get_org(db: AsyncSession, organization_id: int) -> models.Organization:
    org = await db.get(models.Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    return org


@router.get("/organizations", response_model=list[AdminOrganizationPublic])
async def list_organizations(
    admin: PlatformAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
    q: str | None = Query(default=None, max_length=100, description="Поиск по названию, slug или email владельца"),
    status_filter: SubscriptionStatus | None = Query(default=None, alias="status"),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
):
    stmt = select(models.Organization).order_by(models.Organization.id.desc())
    if q and q.strip():
        pattern = "%" + q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        owner_matches = exists(
            select(models.Membership.id)
            .join(models.User, models.User.id == models.Membership.user_id)
            .where(
                models.Membership.organization_id == models.Organization.id,
                models.Membership.role == models.MembershipRole.owner,
                models.User.email.ilike(pattern, escape="\\"),
            ),
        )
        stmt = stmt.where(or_(
            models.Organization.name.ilike(pattern, escape="\\"),
            models.Organization.slug.ilike(pattern, escape="\\"),
            owner_matches,
        ))
    orgs = (await db.execute(stmt)).scalars().all()
    # статус считается в Python, поэтому фильтр по нему применяем до нарезки на страницы
    if status_filter is not None:
        orgs = [o for o in orgs if get_subscription(o).status == status_filter]
    page = orgs[skip:skip + limit]
    owners = await _owner_emails(db, [o.id for o in page])
    return [_public(o, owners.get(o.id)) for o in page]


@router.get("/organizations/{organization_id}", response_model=AdminOrganizationDetail)
async def get_organization(
    organization_id: int, admin: PlatformAdmin, db: Annotated[AsyncSession, Depends(get_db)],
):
    org = await _get_org(db, organization_id)
    owners = await _owner_emails(db, [org.id])
    payments = (await db.execute(
        select(models.Payment)
        .where(models.Payment.organization_id == org.id)
        .order_by(models.Payment.paid_at.desc(), models.Payment.id.desc()),
    )).scalars().all()
    return AdminOrganizationDetail(
        **_public(org, owners.get(org.id)).model_dump(),
        payments=[PaymentPublic.model_validate(p) for p in payments],
    )


@router.patch("/organizations/{organization_id}", response_model=AdminOrganizationPublic)
async def update_organization(
    organization_id: int,
    payload: AdminOrganizationUpdate,
    admin: PlatformAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Ручные правки: выдать бесплатный доступ, приостановить, сменить тариф, поправить даты, оставить заметку."""
    org = await _get_org(db, organization_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(org, field, value.value if hasattr(value, "value") else value)
    await db.commit()
    await db.refresh(org)
    owners = await _owner_emails(db, [org.id])
    return _public(org, owners.get(org.id))


@router.post(
    "/organizations/{organization_id}/payments",
    response_model=PaymentResult,
    status_code=status.HTTP_201_CREATED,
)
async def add_payment(
    organization_id: int,
    payload: PaymentCreate,
    admin: PlatformAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Записать платёж (наличные, перевод) или бесплатный период (method=grant, amount=0) и продлить подписку."""
    org, payment = await record_payment(db, organization_id, admin.id, payload)
    await db.commit()
    await db.refresh(org)
    await db.refresh(payment)
    owners = await _owner_emails(db, [org.id])
    return PaymentResult(payment=PaymentPublic.model_validate(payment), organization=_public(org, owners.get(org.id)))
