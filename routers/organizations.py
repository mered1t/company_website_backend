from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser
from common import generate_unique_slug
from db.database import get_db
from schemas.schemas import OrganizationCreate, OrganizationPublic

router = APIRouter()


@router.post("", response_model=OrganizationPublic, status_code=status.HTTP_201_CREATED)
async def create_organization(
    org: OrganizationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    slug = await generate_unique_slug(db, models.Organization, org.name)

    new_org = models.Organization(name=org.name, slug=slug)
    db.add(new_org)
    await db.flush()

    membership = models.Membership(
        user_id=current_user.id,
        organization_id=new_org.id,
        role=models.MembershipRole.owner,
    )
    db.add(membership)

    await db.commit()
    await db.refresh(new_org)
    return new_org


@router.get("", response_model=list[OrganizationPublic])
async def list_my_organizations(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Organization)
        .join(models.Membership, models.Membership.organization_id == models.Organization.id)
        .where(models.Membership.user_id == current_user.id),
    )
    return result.scalars().all()