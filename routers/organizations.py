from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser, CurrentMembership, require_role
from common import generate_unique_slug, generate_invitation_token
from db.database import get_db
from schemas.schemas import OrganizationCreate, OrganizationPublic, InvitationCreate, InvitationPublic

from datetime import datetime as dt, timedelta

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



@router.post(
    "/{organization_id}/invitations",
    response_model=InvitationPublic,
    status_code=status.HTTP_201_CREATED,
)
async def create_invitation(
    organization_id: int,
    invitation: InvitationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    new_invitation = models.Invitation(
        organization_id=organization_id,
        email=invitation.email.lower(),
        role=models.MembershipRole(invitation.role),
        token=generate_invitation_token(),
        expires_at=dt.now() + timedelta(days=7),
    )
    db.add(new_invitation)
    await db.commit()
    await db.refresh(new_invitation)

    # TODO: отправить email через Resend на следующем шаге

    return new_invitation