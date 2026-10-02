import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status, Query, UploadFile
from fastapi.responses import StreamingResponse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError
from pydantic import ValidationError

import models
from auth.auth import CurrentMembership, require_role, CurrentUser
from auth.auth import ManagerMembership, OwnerMembership
from db.database import get_db
from schemas.schemas import (AppointmentWithDetails,
                             ClientCreate,
                             ClientPublic,
                             ClientUpdate,
                             ActivityLogPublic,
                             ClientImportError,
                             ClientImportResult,
                             ClientCommentCreate,
                             ClientCommentPublic,
                             ClientCommentUpdate)

from datetime import datetime as dt
from common import (get_owned,
                    log_activity,
                    check_no_active_appointments,
                    get_owned_active,
                    restore_entity,
                    check_no_history)

from enums import AppointmentStatus

router = APIRouter()


@router.post("", response_model=ClientPublic, status_code=status.HTTP_201_CREATED)
async def create_client(
    client: ClientCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    new_client = models.Client(
        organization_id=membership.organization_id,
        **client.model_dump(),
    )
    db.add(new_client)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A client with this phone number already exists")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="client", entity_id=new_client.id,
        details=f"Created client {new_client.full_name}",
    )

    await db.commit()
    await db.refresh(new_client)
    return new_client


@router.get("", response_model=list[ClientPublic])
async def list_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    q: str | None = None,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    query = select(models.Client).where(
        models.Client.organization_id == membership.organization_id,
        models.Client.deleted_at.is_(None),
    )

    if q:
        search_term = f"%{q}%"
        query = query.where(
            models.Client.full_name.ilike(search_term) | models.Client.phone.ilike(search_term),
        )

    query = query.offset(skip).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/export")
async def export_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
):
    result = await db.execute(
        select(models.Client).where(
            models.Client.organization_id == membership.organization_id,
            models.Client.deleted_at.is_(None),
        ),
    )
    clients = result.scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["full_name", "phone", "email", "birth_date", "notes"])
    for c in clients:
        writer.writerow([
            c.full_name,
            c.phone,
            c.email or "",
            c.birth_date.date().isoformat() if c.birth_date else "",
            c.notes or "",
        ])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=clients.csv"},
    )


@router.post("/import", response_model=ClientImportResult)
async def import_clients(
    file: UploadFile,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: ManagerMembership,
):
    if not file.filename.endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be a .csv file")

    raw = await file.read()
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    required_columns = {"full_name", "phone"}
    if not required_columns.issubset(set(reader.fieldnames or [])):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"CSV must contain columns: {', '.join(required_columns)}",
        )

    created = 0
    skipped_duplicates = 0
    errors: list[ClientImportError] = []

    for i, row in enumerate(reader, start=2):
        try:
            client_data = ClientCreate(
                full_name=(row.get("full_name") or "").strip(),
                phone=(row.get("phone") or "").strip(),
                email=(row.get("email") or "").strip() or None,
                birth_date=(row.get("birth_date") or "").strip() or None,
                notes=(row.get("notes") or "").strip() or None,
            )
        except ValidationError as e:
            errors.append(ClientImportError(row=i, error=e.errors()[0]["msg"]))
            continue

        existing = await db.execute(
            select(models.Client).where(
                models.Client.organization_id == membership.organization_id,
                models.Client.phone == client_data.phone,
                models.Client.deleted_at.is_(None),
            ),
        )
        if existing.scalars().first():
            skipped_duplicates += 1
            continue

        db.add(models.Client(organization_id=membership.organization_id, **client_data.model_dump()))
        created += 1

    await db.commit()
    await log_activity(
        db, membership.organization_id, membership.user_id,
        action="created", entity_type="client_import", entity_id=0,
        details=f"Imported {created} clients from CSV",
    )
    await db.commit()

    return ClientImportResult(created=created, skipped_duplicates=skipped_duplicates, errors=errors)


@router.get("/{client_id}", response_model=ClientPublic)
async def get_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    result = await db.execute(
        select(models.Client).where(
            models.Client.id == client_id,
            models.Client.organization_id == membership.organization_id,
            models.Client.deleted_at.is_(None),
        ),
    )
    client = result.scalars().first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    return client


@router.patch("/{client_id}", response_model=ClientPublic)
async def update_client(
    client_id: int,
    client_update: ClientUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    client = await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    update_data = client_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(client, field, value)

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="updated", entity_type="client", entity_id=client_id,
        details=f"Updated fields: {', '.join(update_data.keys())}",
    )

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="A client with this phone number already exists")
    await db.refresh(client)
    return client


@router.post("/{client_id}/restore", response_model=ClientPublic)
async def restore_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: ManagerMembership,
):
    client = await restore_entity(db, models.Client, client_id, membership.organization_id, "Client")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="restored", entity_type="client", entity_id=client_id,
        details=f"Restored client {client.full_name}",
    )

    await db.commit()
    await db.refresh(client)
    return client


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: ManagerMembership,
):
    client = await get_owned(db, models.Client, client_id, membership.organization_id, "Client")

    await check_no_active_appointments(db, "client_id", client_id,
                                       "client", membership.organization_id)

    client.deleted_at = dt.now()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="client", entity_id=client_id,
        details=f"Deleted client {client.full_name}",
    )

    await db.commit()


@router.delete("/{client_id}/permanent", status_code=status.HTTP_204_NO_CONTENT)
async def hard_delete_client(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: OwnerMembership,
):
    result = await db.execute(
        select(models.Client).where(
            models.Client.id == client_id,
            models.Client.organization_id == membership.organization_id,
        ),
    )
    client = result.scalars().first()
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Client not found")
    if client.deleted_at is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Client must be soft-deleted first")

    await check_no_history(db, "client_id", client_id, "client")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="hard_deleted", entity_type="client", entity_id=client_id,
        details=f"Permanently deleted client {client.full_name}",
    )

    await db.delete(client)
    await db.commit()


@router.get("/{client_id}/appointments", response_model=list[AppointmentWithDetails])
async def get_client_appointments(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    status_filter: AppointmentStatus | None = None,
):
    await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    query = (
        select(models.Appointment)
        .options(
            selectinload(models.Appointment.client),
            selectinload(models.Appointment.service),
            selectinload(models.Appointment.master),
        )
        .where(
            models.Appointment.client_id == client_id,
            models.Appointment.organization_id == membership.organization_id,
            models.Appointment.deleted_at.is_(None),
        )
    )
    if status_filter is not None:
        query = query.where(models.Appointment.status == status_filter)

    query = query.order_by(models.Appointment.start_time.desc())
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/{client_id}/activity", response_model=list[ActivityLogPublic])
async def get_client_activity(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
):
    await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    appointment_ids_result = await db.execute(
        select(models.Appointment.id).where(models.Appointment.client_id == client_id),
    )
    appointment_ids = [row[0] for row in appointment_ids_result.all()]

    conditions = models.ActivityLog.entity_type == "client", models.ActivityLog.entity_id == client_id
    if appointment_ids:
        appointment_condition = (
            models.ActivityLog.entity_type == "appointment"
        ) & (models.ActivityLog.entity_id.in_(appointment_ids))
        query_filter = (conditions[0] & conditions[1]) | appointment_condition
    else:
        query_filter = conditions[0] & conditions[1]

    result = await db.execute(
        select(models.ActivityLog)
        .where(
            models.ActivityLog.organization_id == membership.organization_id,
            query_filter,
        )
        .order_by(models.ActivityLog.created_at.desc())
        .offset(skip)
        .limit(limit),
    )
    return result.scalars().all()


@router.post("/{client_id}/comments", response_model=ClientCommentPublic, status_code=status.HTTP_201_CREATED)
async def add_client_comment(
    client_id: int,
    payload: ClientCommentCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    comment = models.ClientComment(
        client_id=client_id,
        user_id=current_user.id,
        content=payload.content,
    )
    db.add(comment)
    await db.flush()

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="created", entity_type="client_comment", entity_id=comment.id,
        details=f"Added comment to client #{client_id}",
    )

    await db.commit()
    await db.refresh(comment)

    return ClientCommentPublic(
        id=comment.id,
        content=comment.content,
        author_username=current_user.username,
        created_at=comment.created_at,
    )


@router.get("/{client_id}/comments", response_model=list[ClientCommentPublic])
async def list_client_comments(
    client_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    membership: CurrentMembership,
):
    await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    result = await db.execute(
        select(models.ClientComment, models.User.username)
        .outerjoin(models.User, models.User.id == models.ClientComment.user_id)
        .where(models.ClientComment.client_id == client_id)
        .order_by(models.ClientComment.created_at.desc()),
    )
    return [
        ClientCommentPublic(id=c.id, content=c.content, author_username=username, created_at=c.created_at)
        for c, username in result.all()
    ]


@router.patch("/{client_id}/comments/{comment_id}", response_model=ClientCommentPublic)
async def update_client_comment(
    client_id: int,
    comment_id: int,
    payload: ClientCommentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: CurrentMembership,
):
    await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    result = await db.execute(
        select(models.ClientComment).where(
            models.ClientComment.id == comment_id,
            models.ClientComment.client_id == client_id,
        ),
    )
    comment = result.scalars().first()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

    is_owner_or_admin = membership.role in (models.MembershipRole.owner, models.MembershipRole.admin)
    is_author = comment.user_id == current_user.id
    if not (is_owner_or_admin or is_author):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only edit your own comments")

    comment.content = payload.content

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="updated", entity_type="client_comment", entity_id=comment.id,
        details=f"Edited comment on client #{client_id}",
    )

    await db.commit()
    await db.refresh(comment)

    author_result = await db.execute(select(models.User.username).where(models.User.id == comment.user_id))
    author_username = author_result.scalar()

    return ClientCommentPublic(
        id=comment.id,
        content=comment.content,
        author_username=author_username,
        created_at=comment.created_at,
    )


@router.delete("/{client_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_client_comment(
    client_id: int,
    comment_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
    membership: ManagerMembership,
):
    await get_owned_active(db, models.Client, client_id, membership.organization_id, "Client")

    result = await db.execute(
        select(models.ClientComment).where(
            models.ClientComment.id == comment_id,
            models.ClientComment.client_id == client_id,
        ),
    )
    comment = result.scalars().first()
    if not comment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Comment not found")

    await log_activity(
        db, membership.organization_id, current_user.id,
        action="deleted", entity_type="client_comment", entity_id=comment_id,
        details=f"Deleted comment on client #{client_id}",
    )

    await db.delete(comment)
    await db.commit()