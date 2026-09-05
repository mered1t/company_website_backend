from typing import Annotated

from fastapi import APIRouter, Depends, status, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser, CurrentMembership, require_role
from common import generate_unique_slug, generate_invitation_token
from db.database import get_db
from schemas.schemas import OrganizationCreate, OrganizationPublic, InvitationCreate, InvitationPublic

from datetime import datetime as dt, timedelta
from email_service import send_invitation_email

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
    if invitation.role == "master" and invitation.master_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="master_id is required when inviting a master")

    if invitation.master_id is not None:
        result = await db.execute(
            select(models.Master).where(
                models.Master.id == invitation.master_id,
                models.Master.organization_id == organization_id,
            ),
        )
        if not result.scalars().first():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found in this organization")

    new_invitation = models.Invitation(
        organization_id=organization_id,
        email=invitation.email.lower(),
        role=models.MembershipRole(invitation.role),
        master_id=invitation.master_id,
        token=generate_invitation_token(),
        expires_at=dt.now() + timedelta(days=7),
    )
    db.add(new_invitation)
    await db.commit()
    await db.refresh(new_invitation)

    org_result = await db.execute(select(models.Organization).where(models.Organization.id == organization_id))
    org = org_result.scalars().first()

    send_invitation_email(
        to_email=new_invitation.email,
        organization_name=org.name,
        token=new_invitation.token,
    )

    return new_invitation