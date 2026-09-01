from datetime import datetime as dt, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import CurrentUser
from common import generate_invitation_token
from db.database import get_db
from schemas.schemas import InvitationCreate, InvitationPreview, InvitationPublic

router = APIRouter()


from sqlalchemy.orm import selectinload


@router.get("/{token}", response_model=InvitationPreview)
async def preview_invitation(token: str, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(models.Invitation)
        .options(selectinload(models.Invitation.organization))
        .where(models.Invitation.token == token),
    )
    invitation = result.scalars().first()
    if not invitation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    valid = not invitation.accepted and invitation.expires_at > dt.now()

    return InvitationPreview(
        organization_name=invitation.organization.name,
        email=invitation.email,
        role=invitation.role.value,
        valid=valid,
    )


@router.get("/me/pending", response_model=list[InvitationPublic])
async def list_pending_invitations(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Invitation).where(
            models.Invitation.email == current_user.email,
            models.Invitation.accepted == False,
            models.Invitation.expires_at > dt.now(),
        ),
    )
    return result.scalars().all()


@router.post("/{invitation_id}/accept", status_code=status.HTTP_204_NO_CONTENT)
async def accept_invitation(
    invitation_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    result = await db.execute(
        select(models.Invitation).where(models.Invitation.id == invitation_id),
    )
    invitation = result.scalars().first()
    if not invitation:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

    if invitation.email.lower() != current_user.email.lower():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This invitation is not for you")

    if invitation.accepted:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation already accepted")

    if invitation.expires_at < dt.now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation expired")

    existing = await db.execute(
        select(models.Membership).where(
            models.Membership.user_id == current_user.id,
            models.Membership.organization_id == invitation.organization_id,
        ),
    )
    if existing.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Already a member of this organization")

    membership = models.Membership(
        user_id=current_user.id,
        organization_id=invitation.organization_id,
        role=invitation.role,
    )
    db.add(membership)
    invitation.accepted = True

    await db.commit()