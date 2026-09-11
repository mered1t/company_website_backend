from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated
from auth.auth import hash_password, CurrentUser, create_refresh_token

from fastapi.security import OAuth2PasswordRequestForm
from auth.auth import hash_password, verify_password, create_access_token, verify_access_token, oauth2_scheme
from common import generate_unique_slug

from db.database import get_db
from models import models
from schemas.schemas import (
    UserPublic,
    UserCreate,
    UserPrivate,
    UserUpdate,
    Token,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    RefreshRequest,)

from rate_limiter import limiter

import secrets
from datetime import datetime as dt, timedelta
from email_service import send_password_reset_email

router = APIRouter()


@router.post("", response_model=UserPrivate, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def create_user(request: Request, user: UserCreate, db: Annotated[AsyncSession, Depends(get_db)]):
    result = await db.execute(
        select(models.User).where(func.lower(models.User.username) == user.username.lower()),
    )
    existing_user = result.scalars().first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Username already exists",
        )

    result = await db.execute(
        select(models.User).where(func.lower(models.User.email) == user.email.lower()),
    )
    existing_email = result.scalars().first()
    if existing_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Email already registered",
        )

    new_user = models.User(
        username=user.username,
        email=user.email.lower(),
        password_hash=hash_password(user.password),
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return new_user

@router.get("/me", response_model=UserPrivate)
async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Get the currently authenticated user."""
    user_id = verify_access_token(token)
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Validate user_id is a valid integer (defense against malformed JWT)
    try:
        user_id_int = int(user_id)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(
        select(models.User).where(models.User.id == user_id_int),
    )
    user = result.scalars().first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@router.post("/token", response_model=Token)
@limiter.limit("5/minute")
async def login(
    request: Request,
    form_data: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.User).where(func.lower(models.User.email) == form_data.username.lower()),
    )
    user = result.scalars().first()

    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": str(user.id)})

    refresh_token_value = create_refresh_token()
    refresh_token = models.RefreshToken(
        user_id=user.id,
        token=refresh_token_value,
        expires_at=dt.now() + timedelta(days=30),
    )
    db.add(refresh_token)
    await db.commit()

    return Token(access_token=access_token, refresh_token=refresh_token_value)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.User).where(func.lower(models.User.email) == payload.email.lower()),
    )
    user = result.scalars().first()

    if user:
        token = secrets.token_urlsafe(32)
        reset_token = models.PasswordResetToken(
            user_id=user.id,
            token=token,
            expires_at=dt.now() + timedelta(minutes=30),
        )
        db.add(reset_token)
        await db.commit()

        send_password_reset_email(to_email=user.email, token=token)

    # Всегда одинаковый ответ, независимо от того, найден email или нет


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    payload: ResetPasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.PasswordResetToken).where(models.PasswordResetToken.token == payload.token),
    )
    reset_token = result.scalars().first()

    if not reset_token or reset_token.used or reset_token.expires_at < dt.now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    user_result = await db.execute(select(models.User).where(models.User.id == reset_token.user_id))
    user = user_result.scalars().first()

    user.password_hash = hash_password(payload.new_password)
    reset_token.used = True

    await db.commit()


@router.patch("/{user_id}", response_model=UserPrivate)
async def update_user(
    user_id: int,
    user_update: UserUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    if user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to edit this user")

    result = await db.execute(select(models.User).where(models.User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    if user_update.username is not None:
        user.username = user_update.username
    if user_update.email is not None:
        user.email = user_update.email.lower()

    await db.commit()
    await db.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
    user_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    if user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed to delete this user")

    result = await db.execute(select(models.User).where(models.User.id == user_id))
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    await db.delete(user)
    await db.commit()


@router.post("/refresh", response_model=Token)
async def refresh_access_token(
    payload: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.RefreshToken).where(models.RefreshToken.token == payload.refresh_token),
    )
    refresh_token = result.scalars().first()

    if not refresh_token or refresh_token.revoked or refresh_token.expires_at < dt.now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    access_token = create_access_token(data={"sub": str(refresh_token.user_id)})
    return Token(access_token=access_token, refresh_token=payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.RefreshToken).where(models.RefreshToken.token == payload.refresh_token),
    )
    refresh_token = result.scalars().first()
    if refresh_token:
        refresh_token.revoked = True
        await db.commit()