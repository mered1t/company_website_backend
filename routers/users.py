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
from pydantic import BaseModel, Field, ValidationError
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
    if not await _apply_password_reset(db, payload.token, payload.new_password):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid or expired token")


async def _apply_password_reset(db: AsyncSession, raw_token: str, new_password: str) -> bool:
    """Ставит новый пароль по ссылке из письма. False — ссылка неизвестна, использована или истекла."""
    # «Занимаем» ссылку одним UPDATE: два одновременных запроса с одной ссылкой не пройдут оба
    claimed = await db.execute(
        update(models.PasswordResetToken)
        .where(
            models.PasswordResetToken.token == hash_token(raw_token),
            models.PasswordResetToken.used.is_(False),
            models.PasswordResetToken.expires_at >= utc_now(),
        )
        .values(used=True)
        .returning(models.PasswordResetToken.user_id),
    )
    user_id = claimed.scalar_one_or_none()
    if user_id is None:
        await db.rollback()
        return False

    user = await db.get(models.User, user_id)
    user.password_hash = await hash_password_async(new_password)
    # Кто бы ни был залогинен со старым паролем, его выкидывает со всех устройств; другие ссылки сброса тоже гаснут
    await _end_all_sessions(db, user_id)
    await db.commit()
    return True


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


# Страницы по ссылкам из писем (подтверждение почты, новый пароль) — в том же тёмном стиле, что и письма.
_PAGE_STYLE = """
:root{color-scheme:dark}
body{font-family:'Geist',-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;background:#0A0B10;margin:0;
display:flex;min-height:100vh;align-items:center;justify-content:center;padding:16px;box-sizing:border-box}
.card{background:#13141D;border:1px solid #23253A;border-radius:20px;padding:32px;max-width:420px;width:100%;
box-sizing:border-box}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:28px;color:#ECEDF5;font-weight:600;font-size:15px}
.logo{width:28px;height:28px;border-radius:8px;background:#6A5AF0;color:#fff;display:flex;align-items:center;
justify-content:center;font-weight:700;font-size:14px}
h1{font-size:22px;line-height:1.3;color:#FFFFFF;margin:0 0 10px;font-weight:600}
p{color:#A4A7BE;line-height:1.55;margin:0 0 24px;font-size:15px}
label{display:block;color:#A4A7BE;font-size:13px;margin:0 0 6px}
input[type=password]{width:100%;box-sizing:border-box;background:#0E0F17;border:1px solid #2A2C40;border-radius:10px;
padding:12px 14px;color:#ECEDF5;font-size:15px;margin:0 0 16px;outline:none}
input[type=password]:focus{border-color:#6A5AF0}
.hint{font-size:13px;color:#7D8099;margin:-6px 0 20px}
.error{background:#2A1620;border:1px solid #5A2735;color:#F3A7B5;border-radius:10px;padding:10px 12px;
font-size:14px;margin:0 0 18px}
button,.button{background:#6A5AF0;color:#fff;border:0;border-radius:12px;padding:13px 22px;font-size:15px;
font-weight:600;cursor:pointer;text-decoration:none;display:inline-block;width:100%;box-sizing:border-box;
text-align:center}
button:hover,.button:hover{background:#7B6CF5}
"""


def _page(language: str, title: str, body: str) -> HTMLResponse:
    from html import escape
    app = escape(settings.app_name)
    html = f"""<!doctype html><html lang="{language}"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="robots" content="noindex">
<title>{escape(title)} · {app}</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card"><div class="brand"><span class="logo">{escape((settings.app_name or "C")[:1].upper())}</span>{app}</div>
<h1>{escape(title)}</h1>{body}</div></body></html>"""
    return HTMLResponse(html, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


def _go_to_app(language: str) -> str:
    from html import escape
    link = escape(settings.frontend_url.rstrip("/") + "/")
    return f'<a class="button" href="{link}">{escape(_VERIFY_TEXTS[language][7].format(app=settings.app_name))}</a>'


def _verify_page(language: str, title: str, text: str, *, form_action: str | None = None,
                 token: str | None = None, button: str | None = None) -> HTMLResponse:
    from html import escape
    if form_action:
        action = (f'<form method="post" action="{escape(form_action)}">'
                  f'<input type="hidden" name="token" value="{escape(token or "")}">'
                  f'<button type="submit">{escape(button or "")}</button></form>')
    else:
        action = _go_to_app(language)
    return _page(language, title, f"<p>{escape(text)}</p>{action}")


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


# ------------------------------------------------------------------ страница «новый пароль» для ссылки из письма
# Как и подтверждение почты: GET показывает форму (ссылка сама ничего не меняет), пароль ставит только отправка формы.
_RESET_TEXTS = {
    "en": dict(title="Set a new password", text="Enter a new password for your account.", new="New password",
               repeat="Repeat the password", hint="At least 8 characters, one capital letter and one digit.",
               button="Save password", mismatch="The passwords don't match.",
               weak="The password must be at least 8 characters and contain a capital letter and a digit.",
               done_title="Password changed", done="Your new password is saved. You've been signed out on all devices — sign in with the new password.",
               bad_title="Link is no longer valid", bad="This link has expired or was already used. Request a new one on the sign-in page."),
    "pl": dict(title="Ustaw nowe hasło", text="Wpisz nowe hasło do swojego konta.", new="Nowe hasło",
               repeat="Powtórz hasło", hint="Co najmniej 8 znaków, jedna wielka litera i jedna cyfra.",
               button="Zapisz hasło", mismatch="Hasła nie są takie same.",
               weak="Hasło musi mieć co najmniej 8 znaków, wielką literę i cyfrę.",
               done_title="Hasło zmienione", done="Nowe hasło zostało zapisane. Wylogowaliśmy Cię ze wszystkich urządzeń — zaloguj się nowym hasłem.",
               bad_title="Link jest nieaktualny", bad="Link wygasł lub został już użyty. Poproś o nowy na stronie logowania."),
    "es": dict(title="Crea una nueva contraseña", text="Introduce una nueva contraseña para tu cuenta.",
               new="Nueva contraseña", repeat="Repite la contraseña",
               hint="Al menos 8 caracteres, una mayúscula y un número.", button="Guardar contraseña",
               mismatch="Las contraseñas no coinciden.",
               weak="La contraseña debe tener al menos 8 caracteres, una mayúscula y un número.",
               done_title="Contraseña cambiada", done="Tu nueva contraseña está guardada. Hemos cerrado tu sesión en todos los dispositivos: entra con la nueva contraseña.",
               bad_title="El enlace ya no es válido", bad="El enlace ha caducado o ya se usó. Solicita uno nuevo en la página de acceso."),
    "uk": dict(title="Новий пароль", text="Введіть новий пароль для вашого акаунта.", new="Новий пароль",
               repeat="Повторіть пароль", hint="Щонайменше 8 символів, одна велика літера й одна цифра.",
               button="Зберегти пароль", mismatch="Паролі не збігаються.",
               weak="Пароль має містити щонайменше 8 символів, велику літеру й цифру.",
               done_title="Пароль змінено", done="Новий пароль збережено. Ми вийшли з акаунта на всіх пристроях — увійдіть з новим паролем.",
               bad_title="Посилання недійсне", bad="Посилання застаріло або вже використане. Запросіть нове на сторінці входу."),
    "sq": dict(title="Vendos një fjalëkalim të ri", text="Shkruaj një fjalëkalim të ri për llogarinë tënde.",
               new="Fjalëkalimi i ri", repeat="Përsërit fjalëkalimin",
               hint="Të paktën 8 karaktere, një shkronjë e madhe dhe një shifër.", button="Ruaj fjalëkalimin",
               mismatch="Fjalëkalimet nuk përputhen.",
               weak="Fjalëkalimi duhet të ketë të paktën 8 karaktere, një shkronjë të madhe dhe një shifër.",
               done_title="Fjalëkalimi u ndryshua", done="Fjalëkalimi i ri u ruajt. Të kemi nxjerrë nga të gjitha pajisjet — hyr me fjalëkalimin e ri.",
               bad_title="Lidhja nuk është më e vlefshme", bad="Lidhja ka skaduar ose është përdorur. Kërko një të re në faqen e hyrjes."),
}


async def _reset_link(db: AsyncSession, raw_token: str) -> tuple[bool, str]:
    """(ссылка ещё действует, язык пользователя). Язык известен и у использованной ссылки — для текста ошибки."""
    row = (await db.execute(
        select(models.PasswordResetToken.used, models.PasswordResetToken.expires_at, models.User.language)
        .join(models.User, models.User.id == models.PasswordResetToken.user_id)
        .where(models.PasswordResetToken.token == hash_token(raw_token)),
    )).first()
    if row is None:
        return False, "en"
    language = row.language if row.language in _RESET_TEXTS else "en"
    return (not row.used and row.expires_at >= utc_now()), language


def _reset_form(language: str, action: str, token: str, error: str | None = None) -> HTMLResponse:
    from html import escape
    t = _RESET_TEXTS[language]
    error_html = f'<div class="error">{escape(error)}</div>' if error else ""
    body = f"""<p>{escape(t["text"])}</p>{error_html}
<form method="post" action="{escape(action)}">
<input type="hidden" name="token" value="{escape(token)}">
<label for="p1">{escape(t["new"])}</label>
<input id="p1" type="password" name="new_password" autocomplete="new-password" minlength="8" maxlength="128" required>
<label for="p2">{escape(t["repeat"])}</label>
<input id="p2" type="password" name="repeat_password" autocomplete="new-password" minlength="8" maxlength="128" required>
<div class="hint">{escape(t["hint"])}</div>
<button type="submit">{escape(t["button"])}</button></form>"""
    return _page(language, t["title"], body)


def _reset_result(language: str, ok: bool) -> HTMLResponse:
    from html import escape
    t = _RESET_TEXTS[language]
    title, text = (t["done_title"], t["done"]) if ok else (t["bad_title"], t["bad"])
    return _page(language, title, f"<p>{escape(text)}</p>{_go_to_app(language)}")


@router.get("/reset-password", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("30/minute")
async def reset_password_page(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: str = Query(default="", max_length=200),
):
    """Страница по ссылке из письма «Сброс пароля»: форма нового пароля на языке пользователя."""
    valid, language = await _reset_link(db, token)
    if not valid:
        return _reset_result(language, ok=False)
    return _reset_form(language, f"{request.url.path}/confirm", token)


@router.post("/reset-password/confirm", response_class=HTMLResponse, include_in_schema=False)
@limiter.limit("10/minute")
async def reset_password_confirm(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str, Form(max_length=200)] = "",
    new_password: Annotated[str, Form(max_length=MAX_PASSWORD_LENGTH)] = "",
    repeat_password: Annotated[str, Form(max_length=MAX_PASSWORD_LENGTH)] = "",
):
    valid, language = await _reset_link(db, token)
    if not valid:
        return _reset_result(language, ok=False)
    t = _RESET_TEXTS[language]
    if new_password != repeat_password:
        return _reset_form(language, request.url.path, token, t["mismatch"])
    try:  # те же правила пароля, что и у JSON-эндпоинта
        ResetPasswordRequest(token=token, new_password=new_password)
    except ValidationError:
        return _reset_form(language, request.url.path, token, t["weak"])
    return _reset_result(language, ok=await _apply_password_reset(db, token, new_password))
