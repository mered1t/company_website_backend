from typing import Annotated

from fastapi import APIRouter, Depends, status, HTTPException, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser, CurrentMembership, require_role
from common import generate_unique_slug, generate_invitation_token, log_activity
from db.database import get_db
from schemas.schemas import (OrganizationCreate,
                             OrganizationPublic,
                             InvitationCreate,
                             InvitationPublic,
                             OrganizationWithRole,
                             MemberPublic, ActivityLogPublic)

from datetime import datetime as dt, timedelta
from email_service import send_invitation_email
from rate_limiter import limiter

router = APIRouter()


@router.get("/{organization_id}/members", response_model=list[MemberPublic])
async def list_members(
    organization_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.User, models.Membership.role, models.Membership.master_id)
        .join(models.Membership, models.Membership.user_id == models.User.id)
        .where(models.Membership.organization_id == organization_id),
    )
    rows = result.all()

    return [
        MemberPublic(
            user_id=user.id,
            username=user.username,
            email=user.email,
            role=role.value,
            master_id=master_id,
        )
        for user, role, master_id in rows
    ]


@router.post("", response_model=OrganizationPublic, status_code=status.HTTP_201_CREATED)
async def create_organization(
    org: OrganizationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    slug = await generate_unique_slug(db, models.Organization, org.name)

    new_org = models.Organization(
        name=org.name,
        slug=slug,
        timezone=org.timezone or "UTC",
    )
    
    db.add(new_org)
    await db.flush()

    membership = models.Membership(
        user_id=current_user.id,
        organization_id=new_org.id,
        role=models.MembershipRole.owner,
    )
    db.add(membership)

    await log_activity(
        db, new_org.id, current_user.id,
        action="created", entity_type="organization", entity_id=new_org.id,
        details=f"Created organization {new_org.name}",
    )

    await db.commit()
    await db.refresh(new_org)
    return new_org


@router.get("", response_model=list[OrganizationWithRole])
async def list_my_organizations(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Organization, models.Membership.role, models.Membership.master_id)
        .join(models.Membership, models.Membership.organization_id == models.Organization.id)
        .where(models.Membership.user_id == current_user.id),
    )
    rows = result.all()

    return [
        OrganizationWithRole(
            id=org.id,
            name=org.name,
            slug=org.slug,
            created_at=org.created_at,
            role=role.value,
            master_id=master_id,
        )
        for org, role, master_id in rows
    ]



@router.post(
    "/{organization_id}/invitations",
    response_model=InvitationPublic,
    status_code=status.HTTP_201_CREATED,
)


@limiter.limit("10/hour")
async def create_invitation(
    request: Request,
    organization_id: int,
    invitation: InvitationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):

    if not current_user.email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email before inviting team members",
        )

    if invitation.role == "master" and invitation.master_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="master_id is required when inviting a master")

    if invitation.master_id is not None:
        result = await db.execute(
            select(models.Master).where(
                models.Master.id == invitation.master_id,
                models.Master.organization_id == organization_id,
                models.Master.deleted_at.is_(None),
            ),
        )
        if not result.scalars().first():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Master not found in this organization")

    existing_membership = await db.execute(
        select(models.Membership)
        .join(models.User, models.User.id == models.Membership.user_id)
        .where(
            models.Membership.organization_id == organization_id,
            func.lower(models.User.email) == invitation.email.lower(),
        ),
    )
    if existing_membership.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This person is already a member of the organization")

    existing_invitation = await db.execute(
        select(models.Invitation).where(
            models.Invitation.organization_id == organization_id,
            models.Invitation.email == invitation.email.lower(),
            models.Invitation.accepted == False,
            models.Invitation.expires_at > dt.now(),
        ),
    )
    if existing_invitation.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An active invitation for this email already exists")

    new_invitation = models.Invitation(
        organization_id=organization_id,
        email=invitation.email.lower(),
        role=models.MembershipRole(invitation.role),
        master_id=invitation.master_id,
        token=generate_invitation_token(),
        expires_at=dt.now() + timedelta(days=7),
    )
    db.add(new_invitation)
    await db.flush()

    await log_activity(
        db, organization_id, membership.user_id,
        action="created", entity_type="invitation", entity_id=new_invitation.id,
        details=f"Invited {new_invitation.email} as {new_invitation.role.value}",
    )

    await db.commit()
    await db.refresh(new_invitation)

    org_result = await db.execute(select(models.Organization).where(models.Organization.id == organization_id))
    org = org_result.scalars().first()

    try:
        send_invitation_email(
            to_email=new_invitation.email,
            organization_name=org.name,
            token=new_invitation.token,
        )
    except Exception:
        pass

    return new_invitation


@router.post("/{organization_id}/invitations/{invitation_id}/resend", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def resend_invitation(
    request: Request,
    organization_id: int,
    invitation_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Invitation).where(
            models.Invitation.id == invitation_id,
            models.Invitation.organization_id == organization_id,
        ),
    )
    invitation = result.scalars().first()
    if not invitation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    if invitation.accepted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation already accepted")

    if invitation.expires_at < dt.now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation expired, create a new one")

    org_result = await db.execute(select(models.Organization).where(models.Organization.id == organization_id))
    org = org_result.scalars().first()

    try:
        send_invitation_email(
            to_email=invitation.email,
            organization_name=org.name,
            token=invitation.token,
        )
    except Exception:
        pass

    await log_activity(
        db, organization_id, membership.user_id,
        action="updated", entity_type="invitation", entity_id=invitation.id,
        details=f"Resent invitation to {invitation.email}",
    )
    await db.commit()


@router.get("/{organization_id}/invitations", response_model=list[InvitationPublic])
async def list_invitations(
    organization_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Invitation).where(
            models.Invitation.organization_id == organization_id,
            models.Invitation.accepted == False,
        ),
    )
    return result.scalars().all()


@router.delete("/{organization_id}/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_invitation(
    organization_id: int,
    invitation_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Invitation).where(
            models.Invitation.id == invitation_id,
            models.Invitation.organization_id == organization_id,
        ),
    )
    invitation = result.scalars().first()
    if not invitation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    await log_activity(
        db, organization_id, membership.user_id,
        action="deleted", entity_type="invitation", entity_id=invitation.id,
        details=f"Revoked invitation to {invitation.email}",
    )

    await db.delete(invitation)
    await db.commit()


@router.get("/{organization_id}/activity", response_model=list[ActivityLogPublic])
async def list_activity(
    organization_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    result = await db.execute(
        select(models.ActivityLog)
        .where(models.ActivityLog.organization_id == organization_id)
        .order_by(models.ActivityLog.created_at.desc())
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.delete("/{organization_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    organization_id: int,
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: Annotated[models.Membership, Depends(require_role(models.MembershipRole.owner, models.MembershipRole.admin))],
):
    result = await db.execute(
        select(models.Membership).where(
            models.Membership.organization_id == organization_id,
            models.Membership.user_id == user_id,
        ),
    )
    target_membership = result.scalars().first()
    if not target_membership:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    if target_membership.role == models.MembershipRole.owner:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the organization owner")

    await log_activity(
        db, organization_id, membership.user_id,
        action="deleted", entity_type="member", entity_id=user_id,
    )

    await db.delete(target_membership)
    await db.commit()