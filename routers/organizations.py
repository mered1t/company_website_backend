from typing import Annotated

from fastapi import APIRouter, Depends, status, HTTPException, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

import models
from auth.auth import AnyStatusMembership, OwnerMembership, OwnerMembershipAnyStatus, verify_password_async
from schemas.billing import SubscriptionInfo
from schemas.schemas import TransferOwnershipRequest
from auth.auth import CurrentUser, CurrentMembership
from auth.auth import ManagerMembership
from services.common import generate_unique_slug, generate_invitation_token, log_activity
from db.database import get_db
from schemas import schemas
from schemas.schemas import (OrganizationCreate,
                             OrganizationPublic,
                             InvitationCreate,
                             InvitationPublic,
                             OrganizationWithRole,
                             MemberPublic,
                             ActivityLogPublic)

from datetime import timedelta
from auth.auth import hash_token
from fastapi import BackgroundTasks
from services.email_service import safe_send, send_invitation_email
from core.rate_limiter import limiter
import logging
import re
from core.time_utils import utc_now
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from services.org_data import build_export, delete_organization_data

logger = logging.getLogger(__name__)

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
        currency=org.currency or "EUR",
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


@router.patch("/{organization_id}", response_model=schemas.OrganizationPublic)
async def update_organization(
    organization_id: int,
    payload: schemas.OrganizationUpdate,
        membership: ManagerMembership,
    db: AsyncSession = Depends(get_db),
):
    org = await db.get(models.Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    update_data = payload.model_dump(exclude_unset=True)

    if "slug" in update_data:
        if membership.role != "owner":
            raise HTTPException(403, "Only the owner can change the organization's URL slug")
        new_slug = update_data["slug"]
        if new_slug != org.slug:
            existing = await db.execute(
                select(models.Organization).where(
                    models.Organization.slug == new_slug,
                    models.Organization.id != organization_id,
                )
            )
            if existing.scalar_one_or_none():
                raise HTTPException(400, "This slug is already taken")

    if "currency" in update_data and membership.role != "owner":
        raise HTTPException(403, "Only the owner can change the organization's currency")

    changed_fields = []
    for field, value in update_data.items():
        if getattr(org, field) != value:
            setattr(org, field, value)
            changed_fields.append(field)

    if changed_fields:
        await log_activity(
            db, membership.organization_id, membership.user_id,
            "updated", "organization", org.id,
            f"Updated fields: {', '.join(changed_fields)}",
        )

    await db.commit()
    await db.refresh(org)
    return org


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
            timezone=org.timezone,
            booking_horizon_days=org.booking_horizon_days,
            currency=org.currency,
            plan=org.plan,
            subscription=SubscriptionInfo.from_org(org),
            role=role.value,
            master_id=master_id,
        )
        for org, role, master_id in rows
    ]


@router.get("/{organization_id}/subscription", response_model=SubscriptionInfo)
async def get_subscription_info(organization_id: int, membership: AnyStatusMembership):
    """Состояние подписки организации (триал, оплата, льготный период). Работает и у закрытой организации,
    чтобы фронтенд мог показать экран «оплатите»."""
    return SubscriptionInfo.from_org(membership.organization)



@router.post(
    "/{organization_id}/invitations",
    response_model=InvitationPublic,
    status_code=status.HTTP_201_CREATED,
)


@limiter.limit("10/hour")
async def create_invitation(
    background_tasks: BackgroundTasks,
    request: Request,
    organization_id: int,
    invitation: InvitationCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: ManagerMembership,
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
            models.Invitation.expires_at > utc_now(),
        ),
    )
    if existing_invitation.scalars().first():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An active invitation for this email already exists")

    raw_token = generate_invitation_token()
    new_invitation = models.Invitation(
        language=invitation.language or current_user.language,
        organization_id=organization_id,
        email=invitation.email.lower(),
        role=models.MembershipRole(invitation.role),
        master_id=invitation.master_id,
        token=hash_token(raw_token),
        expires_at=utc_now() + timedelta(days=7),
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

    background_tasks.add_task(
        safe_send,
        send_invitation_email,
        log=("Failed to send invitation email (invitation_id=%s)", new_invitation.id,),
        to_email=new_invitation.email, organization_name=org.name, token=raw_token, language=new_invitation.language,
    )

    return new_invitation


@router.post("/{organization_id}/invitations/{invitation_id}/resend", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def resend_invitation(
    background_tasks: BackgroundTasks,
    request: Request,
    organization_id: int,
    invitation_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
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

    if invitation.expires_at < utc_now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invitation expired, create a new one")

    org_result = await db.execute(select(models.Organization).where(models.Organization.id == organization_id))
    org = org_result.scalars().first()

    # в БД токен хранится хешем, поэтому для повторной отправки выпускаем новый
    raw_token = generate_invitation_token()
    invitation.token = hash_token(raw_token)
    background_tasks.add_task(
        safe_send,
        send_invitation_email,
        log=("Failed to resend invitation email (invitation_id=%s)", invitation.id,),
        to_email=invitation.email, organization_name=org.name, token=raw_token, language=invitation.language,
    )

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
    membership: ManagerMembership,
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
    membership: ManagerMembership,
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
    membership: ManagerMembership,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    result = await db.execute(
        select(models.ActivityLog)
        .where(models.ActivityLog.organization_id == organization_id)
        .order_by(models.ActivityLog.created_at.desc(), models.ActivityLog.id.desc())
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.delete("/{organization_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_member(
    organization_id: int,
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
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


@router.post("/{organization_id}/transfer-ownership", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def transfer_ownership(
    request: Request,
    organization_id: int,
    payload: TransferOwnershipRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: OwnerMembership,
):
    """Передать роль владельца другому участнику. Прежний владелец становится admin.
    Нужен пароль текущего владельца."""
    if not await verify_password_async(payload.password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

    if payload.new_owner_user_id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You are already the owner")

    result = await db.execute(
        select(models.Membership).where(
            models.Membership.organization_id == organization_id,
            models.Membership.user_id == payload.new_owner_user_id,
        ),
    )
    target = result.scalars().first()
    if not target:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found")

    target.role = models.MembershipRole.owner
    membership.role = models.MembershipRole.admin

    await log_activity(
        db, organization_id, current_user.id,
        action="transferred", entity_type="organization", entity_id=organization_id,
        details=f"Ownership transferred to user #{payload.new_owner_user_id}",
    )

    await db.commit()


@router.get("/{organization_id}/export")
@limiter.limit("5/hour")
async def export_organization_data(
    request: Request,
    organization_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: OwnerMembershipAnyStatus,
):
    """Скачать все данные организации одним JSON-файлом (переносимость данных, GDPR). Только владелец."""
    data = await build_export(db, organization_id)

    await log_activity(
        db, organization_id, current_user.id,
        action="exported", entity_type="organization", entity_id=organization_id,
        details="Exported all organization data",
    )
    await db.commit()

    slug = re.sub(r"[^a-z0-9-]", "", data["organization"]["slug"].lower()) or "organization"
    filename = f"{slug}-export-{utc_now():%Y-%m-%d}.json"
    return JSONResponse(
        content=jsonable_encoder(data),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


class OrganizationDeleteRequest(BaseModel):
    password: str
    confirm_name: str  # название организации, набранное вручную: защита от случайного нажатия


@router.post("/{organization_id}/delete", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def delete_organization(
    request: Request,
    organization_id: int,
    payload: OrganizationDeleteRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: OwnerMembershipAnyStatus,
):
    """Безвозвратно удалить организацию со всеми данными (клиенты, записи, мастера, услуги, участники).
    Нужны пароль владельца и название организации. Аккаунты пользователей остаются."""
    if not await verify_password_async(payload.password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

    org = await db.get(models.Organization, organization_id)
    if org is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")
    if payload.confirm_name.strip() != org.name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Organization name does not match")

    user_id = current_user.id
    await delete_organization_data(db, organization_id)
    await db.commit()
    logger.warning("Organization %s was permanently deleted by user %s", organization_id, user_id)
