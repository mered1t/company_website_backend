from fastapi import APIRouter, Depends, Form, HTTPException, Query, status, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select, func, update
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
    ResendVerificationRequest, MAX_PASSWORD_LENGTH,
    ChangePasswordRequest)

from core.rate_limiter import limiter
from core.config import settings
from pydantic import BaseModel, Field
from services.account import OwnsOrganizationError, delete_user_account

import secrets
from datetime import timedelta
from fastapi import BackgroundTasks
from services.email_service import (
    safe_send, send_email_changed_email, send_password_reset_email, send_verification_email,
)
import logging
from core.time_utils import utc_now


router = APIRouter()

logger = logging.getLogger(__name__)

REFRESH_TOKEN_DAYS = 30


async def _issue_refresh_token(db: AsyncSession, user_id: int, expires_at=None) -> str:
    """Новый refresh-токен (в базе только его хеш). Возвращает сам токен для клиента."""
    value = create_refresh_token()
    db.add(models.RefreshToken(
        user_id=user_id,
        token=hash_token(value),
        expires_at=expires_at or utc_now() + timedelta(days=REFRESH_TOKEN_DAYS),
    ))
    return value


async def _end_all_sessions(db: AsyncSession, user_id: int) -> None:
    """Выйти со всех устройств: гасим все refresh-токены и все неиспользованные ссылки сброса пароля."""
    await db.execute(
        update(models.RefreshToken)
        .where(models.RefreshToken.user_id == user_id, models.RefreshToken.revoked.is_(False))
        .values(revoked=True),
    )
    await db.execute(
        update(models.PasswordResetToken)
        .where(models.PasswordResetToken.user_id == user_id, models.PasswordResetToken.used.is_(False))
        .values(used=True),
    )


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
    refresh_token_value = await _issue_refresh_token(db, user.id)
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
    # «Занимаем» ссылку одним UPDATE: два одновременных запроса с одной ссылкой не пройдут оба
    claimed = await db.execute(
        update(models.PasswordResetToken)
        .where(
            models.PasswordResetToken.token == hash_token(payload.token),
            models.PasswordResetToken.used.is_(False),
            models.PasswordResetToken.expires_at >= utc_now(),
        )
        .values(used=True)
        .returning(models.PasswordResetToken.user_id),
    )
    user_id = claimed.scalar_one_or_none()
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")

    user = await db.get(models.User, user_id)
    user.password_hash = await hash_password_async(payload.new_password)
    # Кто бы ни был залогинен со старым паролем, его выкидывает со всех устройств; другие ссылки сброса тоже гаснут
    await _end_all_sessions(db, user_id)
    await db.commit()


@router.post("/me/password", response_model=Token)
@limiter.limit("5/minute")
async def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    """Сменить пароль, зная текущий. Все остальные входы завершаются; в ответе новые токены для этого устройства."""
    if not await verify_password_async(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

    user = await db.get(models.User, current_user.id)
    user.password_hash = await hash_password_async(payload.new_password)
    await _end_all_sessions(db, user.id)
    refresh_token_value = await _issue_refresh_token(db, user.id)
    await db.commit()
    return Token(access_token=create_access_token(data={"sub": str(user.id)}), refresh_token=refresh_token_value)


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
    old_email = None
    if user_update.email is not None and user_update.email.lower() != user.email.lower():
        # без пароля сменить email нельзя: иначе с украденным токеном входа можно забрать аккаунт навсегда
        if not user_update.current_password:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="Current password is required to change email")
        if not await verify_password_async(user_update.current_password, user.password_hash):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

        existing = await db.execute(
            select(models.User).where(func.lower(models.User.email) == user_update.email.lower()),
        )
        if existing.scalars().first():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered")

        old_email = user.email
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
    if old_email:
        background_tasks.add_task(
            safe_send,
            send_email_changed_email,
            log=("Failed to send email-changed notice",),
            to_email=old_email, new_email=user.email, language=user.language,
        )

    return user


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH)


@router.post("/me/delete", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def delete_my_account(
    request: Request,
    payload: DeleteAccountRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: CurrentUser,
):
    """Безвозвратно удалить свой аккаунт. Нужен пароль.
    Владелец организации сначала передаёт владение другому участнику или удаляет организацию."""
    if not await verify_password_async(payload.password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Incorrect password")

    user_id = current_user.id
    try:
        await delete_user_account(db, current_user)
    except OwnsOrganizationError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete account while you own an organization. Transfer ownership or delete the organization first.",
        )
    await db.commit()
    logger.warning("User account %s was permanently deleted", user_id)


@router.post("/refresh", response_model=Token)
@limiter.limit("10/minute")
async def refresh_access_token(
    request: Request,
    payload: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    # Ротация: старый токен гасится одним UPDATE и взамен выдаётся новый с тем же сроком окончания
    # (вход действует не дольше 30 дней с момента логина). Повторно старый токен не сработает.
    rotated = await db.execute(
        update(models.RefreshToken)
        .where(
            models.RefreshToken.token == hash_token(payload.refresh_token),
            models.RefreshToken.revoked.is_(False),
            models.RefreshToken.expires_at >= utc_now(),
        )
        .values(revoked=True)
        .returning(models.RefreshToken.user_id, models.RefreshToken.expires_at),
    )
    row = rotated.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token")

    new_refresh = await _issue_refresh_token(db, row.user_id, expires_at=row.expires_at)
    await db.commit()
    return Token(access_token=create_access_token(data={"sub": str(row.user_id)}), refresh_token=new_refresh)


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


@router.post("/me/logout-all", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def logout_all_devices(
    request: Request,
    current_user: CurrentUser,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    """Выйти со всех устройств, включая это: все refresh-токены гасятся. Уже выданный access-токен доживает свой срок
    (до 30 минут), поэтому фронтенд сразу забывает оба токена и отправляет на страницу входа."""
    await _end_all_sessions(db, current_user.id)
    await db.commit()


async def _verify_email_token(db: AsyncSession, raw_token: str) -> bool:
    """Подтверждает почту по токену из письма. False, если токен неизвестен, использован или истёк."""
    result = await db.execute(
        select(models.EmailVerificationToken).where(models.EmailVerificationToken.token == hash_token(raw_token)),
    )
    verification_token = result.scalars().first()
    if not verification_token or verification_token.used or verification_token.expires_at < utc_now():
        return False
    user = await db.get(models.User, verification_token.user_id)
    user.email_verified = True
    verification_token.used = True
    await db.commit()
    return True


@router.post("/verify-email", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    payload: VerifyEmailRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    if not await _verify_email_token(db, payload.token):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")


# ------------------------------------------------------------------ страница подтверждения для ссылки из письма
# Ссылка ведёт на GET со страницей и кнопкой; подтверждает только нажатие (POST). Так почтовые сканеры
# (Outlook, антивирусы), которые сами открывают ссылки из писем, не тратят токен и ничего не подтверждают.

_VERIFY_TEXTS = {
    "en": ("Confirm your email", "Click the button to confirm your email address.", "Confirm email",
           "Email confirmed", "Thank you! Your email address is confirmed.",
           "Link is no longer valid", "This link has expired or was already used. Request a new email in the app.",
           "Go to {app}"),
    "pl": ("Potwierdź e-mail", "Kliknij przycisk, aby potwierdzić adres e-mail.", "Potwierdź e-mail",
           "E-mail potwierdzony", "Dziękujemy! Twój adres e-mail został potwierdzony.",
           "Link jest nieaktualny", "Link wygasł lub został już użyty. Poproś o nowy e-mail w aplikacji.",
           "Przejdź do {app}"),
    "es": ("Confirma tu correo", "Pulsa el botón para confirmar tu correo electrónico.", "Confirmar correo",
           "Correo confirmado", "¡Gracias! Tu correo electrónico está confirmado.",
           "El enlace ya no es válido", "El enlace ha caducado o ya se usó. Solicita un nuevo correo en la aplicación.",
           "Ir a {app}"),
    "uk": ("Підтвердіть пошту", "Натисніть кнопку, щоб підтвердити електронну пошту.", "Підтвердити пошту",
           "Пошту підтверджено", "Дякуємо! Вашу електронну пошту підтверджено.",
           "Посилання недійсне", "Посилання застаріло або вже використане. Надішліть новий лист у застосунку.",
           "Перейти до {app}"),
    "sq": ("Konfirmo email-in", "Kliko butonin për të konfirmuar adresën e email-it.", "Konfirmo email-in",
           "Email-i u konfirmua", "Faleminderit! Adresa jote e email-it u konfirmua.",
           "Lidhja nuk është më e vlefshme", "Lidhja ka skaduar ose është përdorur. Kërko një email të ri në aplikacion.",
           "Shko te {app}"),
}


async def _token_language(db: AsyncSession, raw_token: str) -> str:
    row = (await db.execute(
        select(models.User.language)
        .join(models.EmailVerificationToken, models.EmailVerificationToken.user_id == models.User.id)
        .where(models.EmailVerificationToken.token == hash_token(raw_token)),
    )).scalar()
    return row if row in _VERIFY_TEXTS else "en"


def _verify_page(language: str, title: str, text: str, *, form_action: str | None = None,
                 token: str | None = None, button: str | None = None) -> HTMLResponse:
    from html import escape
    t = _VERIFY_TEXTS[language]
    if form_action:
        action = (f'<form method="post" action="{escape(form_action)}">'
                  f'<input type="hidden" name="token" value="{escape(token or "")}">'
                  f'<button type="submit">{escape(button or "")}</button></form>')
    else:
        link = escape(settings.frontend_url.rstrip("/") + "/")
        action = f'<a class="button" href="{link}">{escape(t[7].format(app=settings.app_name))}</a>'
    html = f"""<!doctype html><html lang="{language}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{escape(title)} · {escape(settings.app_name)}</title>
<style>body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#f3f4f6;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px}}
.card{{background:#fff;border-radius:12px;padding:32px;max-width:420px;width:100%;box-shadow:0 1px 3px #0001;
text-align:center}}h1{{font-size:20px;color:#111827;margin:0 0 12px}}p{{color:#4b5563;line-height:1.5;margin:0 0 24px}}
button,.button{{background:#111827;color:#fff;border:0;border-radius:8px;padding:12px 24px;font-size:15px;
cursor:pointer;text-decoration:none;display:inline-block}}</style></head>
<body><div class="card"><h1>{escape(title)}</h1><p>{escape(text)}</p>{action}</div></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.get("/verify-email", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("30/minute")
async def verify_email_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str = Query(default="", max_length=200),
):
    """Страница по ссылке из письма: только кнопка, сама ссылка ничего не меняет."""
    language = await _token_language(db, token)
    t = _VERIFY_TEXTS[language]
    return _verify_page(language, t[0], t[1], form_action=f"{request.url.path}/confirm", token=token, button=t[2])


@router.post("/verify-email/confirm", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def verify_email_confirm(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Form(max_length=200)] = "",
):
    language = await _token_language(db, token)
    t = _VERIFY_TEXTS[language]
    if await _verify_email_token(db, token):
        return _verify_page(language, t[3], t[4])
    return _verify_page(language, t[5], t[6])


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