from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy import select, func, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated
from auth.auth import CurrentUser, create_refresh_token

from fastapi.security import OAuth2PasswordRequestForm
from auth.auth import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    hash_password_async,
    hash_token,
    verify_password_async,
)

from db.database import get_db
from models import models
from schemas.schemas import (
    UserCreate,
    UserPrivate,
    UserUpdate,
    Token,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    RefreshRequest,
    VerifyEmailRequest,
    ResendVerificationRequest, MAX_PASSWORD_LENGTH)

from rate_limiter import limiter

import secrets
from datetime import timedelta
from fastapi import BackgroundTasks
from email_service import safe_send, send_password_reset_email, send_verification_email
import logging
from time_utils import utc_now


router = APIRouter()

logger = logging.getLogger(__name__)


@router.post("", response_model=UserPrivate, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def create_user(background_tasks: BackgroundTasks, request: Request, user: UserCreate, db: Annotated[AsyncSession, Depends(get_db)]):
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
        password_hash=await hash_password_async(user.password),
        language=user.language,
        terms_accepted_at=utc_now(),
    )
    db.add(new_user)
    await db.flush()

    token = secrets.token_urlsafe(32)
    verification_token = models.EmailVerificationToken(
        user_id=new_user.id,
        token=hash_token(token),
        expires_at=utc_now() + timedelta(hours=24),
    )
    db.add(verification_token)
    await db.commit()
    await db.refresh(new_user)

    background_tasks.add_task(
        safe_send,
        send_verification_email,
        log=("Failed to send verification email",),
        to_email=new_user.email, token=token, language=new_user.language,
    )

    return new_user


@router.post("/me/accept-terms", response_model=UserPrivate)
async def accept_terms(
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    current_user.terms_accepted_at = utc_now()
    await db.commit()
    await db.refresh(current_user)
    return current_user


@router.get("/me", response_model=UserPrivate)
async def get_me(current_user: CurrentUser):
    return current_user


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

    # Хеш считаем всегда, даже если пользователя нет: иначе по времени ответа
    # можно узнать, зарегистрирован ли email.
    password_ok = await verify_password_async(
        form_data.password,
        user.password_hash if user else DUMMY_PASSWORD_HASH,
    )

    if not user or not password_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if len(form_data.password) > MAX_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(data={"sub": str(user.id)})

    refresh_token_value = create_refresh_token()
    refresh_token = models.RefreshToken(
        user_id=user.id,
        token=hash_token(refresh_token_value),
        expires_at=utc_now() + timedelta(days=30),
    )
    db.add(refresh_token)
    await db.commit()

    return Token(access_token=access_token, refresh_token=refresh_token_value)


@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def forgot_password(
    background_tasks: BackgroundTasks,
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
            token=hash_token(token),
            expires_at=utc_now() + timedelta(minutes=30),
        )
        db.add(reset_token)
        await db.commit()

        background_tasks.add_task(
            safe_send,
            send_password_reset_email,
            log=("Failed to send password reset email",),
            to_email=user.email, token=token, language=user.language,
        )

    # Всегда одинаковый ответ, независимо от того, найден email или нет


@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.PasswordResetToken).where(models.PasswordResetToken.token == hash_token(payload.token)),
    )
    reset_token = result.scalars().first()

    if not reset_token or reset_token.used or reset_token.expires_at < utc_now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    user_result = await db.execute(select(models.User).where(models.User.id == reset_token.user_id))
    user = user_result.scalars().first()

    user.password_hash = await hash_password_async(payload.new_password)
    reset_token.used = True

    await db.commit()


@router.patch("/{user_id}", response_model=UserPrivate)
async def update_user(
    background_tasks: BackgroundTasks,
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

    if user_update.username is not None and user_update.username.lower() != user.username.lower():
        existing = await db.execute(
            select(models.User).where(func.lower(models.User.username) == user_update.username.lower()),
        )
        if existing.scalars().first():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username already exists")
        user.username = user_update.username

    if user_update.language is not None:
        user.language = user_update.language

    new_verification_token = None
    if user_update.email is not None and user_update.email.lower() != user.email.lower():
        existing = await db.execute(
            select(models.User).where(func.lower(models.User.email) == user_update.email.lower()),
        )
        if existing.scalars().first():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

        user.email = user_update.email.lower()
        user.email_verified = False

        # старые токены подтверждения относились к прежнему адресу, гасим их
        await db.execute(
            update(models.EmailVerificationToken)
            .where(
                models.EmailVerificationToken.user_id == user.id,
                models.EmailVerificationToken.used.is_(False),
            )
            .values(used=True),
        )
        new_verification_token = secrets.token_urlsafe(32)
        db.add(models.EmailVerificationToken(
            user_id=user.id,
            token=hash_token(new_verification_token),
            expires_at=utc_now() + timedelta(hours=24),
        ))

    await db.commit()
    await db.refresh(user)

    if new_verification_token:
        background_tasks.add_task(
            safe_send,
            send_verification_email,
            log=("Failed to send verification email after email change",),
            to_email=user.email, token=new_verification_token, language=user.language,
        )

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

    owner_check = await db.execute(
        select(models.Membership).where(
            models.Membership.user_id == user_id,
            models.Membership.role == models.MembershipRole.owner,
        ),
    )
    if owner_check.scalars().first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete account while you own an organization. Transfer ownership or delete the organization first.",
        )

    await db.execute(delete(models.RefreshToken).where(models.RefreshToken.user_id == user_id))
    await db.execute(delete(models.PasswordResetToken).where(models.PasswordResetToken.user_id == user_id))
    await db.execute(delete(models.EmailVerificationToken).where(models.EmailVerificationToken.user_id == user_id))
    await db.execute(delete(models.Membership).where(models.Membership.user_id == user_id))

    await db.delete(user)
    await db.commit()


@router.post("/refresh", response_model=Token)
@limiter.limit("10/minute")
async def refresh_access_token(
    request: Request,
    payload: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.RefreshToken).where(models.RefreshToken.token == hash_token(payload.refresh_token)),
    )
    refresh_token = result.scalars().first()

    if not refresh_token or refresh_token.revoked or refresh_token.expires_at < utc_now():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    access_token = create_access_token(data={"sub": str(refresh_token.user_id)})
    return Token(access_token=access_token, refresh_token=payload.refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def logout(
    request: Request,
    payload: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.RefreshToken).where(models.RefreshToken.token == hash_token(payload.refresh_token)),
    )
    refresh_token = result.scalars().first()
    if refresh_token:
        refresh_token.revoked = True
        await db.commit()


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    payload: VerifyEmailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.EmailVerificationToken).where(models.EmailVerificationToken.token == hash_token(payload.token)),
    )
    verification_token = result.scalars().first()

    if not verification_token or verification_token.used or verification_token.expires_at < utc_now():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    user_result = await db.execute(select(models.User).where(models.User.id == verification_token.user_id))
    user = user_result.scalars().first()

    user.email_verified = True
    verification_token.used = True

    await db.commit()


@router.post("/resend-verification", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def resend_verification(
    background_tasks: BackgroundTasks,
    request: Request,
    payload: ResendVerificationRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    result = await db.execute(
        select(models.User).where(func.lower(models.User.email) == payload.email.lower()),
    )
    user = result.scalars().first()

    if user and not user.email_verified:
        token = secrets.token_urlsafe(32)
        verification_token = models.EmailVerificationToken(
            user_id=user.id,
            token=hash_token(token),
            expires_at=utc_now() + timedelta(hours=24),
        )
        db.add(verification_token)
        await db.commit()

        background_tasks.add_task(
            safe_send,
            send_verification_email,
            log=("Failed to resend verification email",),
            to_email=user.email, token=token, language=user.language,
        )

    # Всегда одинаковый ответ, независимо от результата