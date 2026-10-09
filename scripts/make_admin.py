"""Назначает или снимает права админа платформы (доступ к /api/v1/admin/...).

Запуск (из корня проекта):
    python3 scripts/make_admin.py --list                          # кто сейчас админ
    python3 scripts/make_admin.py --email you@example.com         # сделать админом
    python3 scripts/make_admin.py --email you@example.com --revoke  # снять права

База берётся из DATABASE_URL (.env или переменная окружения). Чтобы сделать это на Render, запусти с адресом боевой базы:
    DATABASE_URL="postgresql+asyncpg://..." python3 scripts/make_admin.py --email you@example.com
Для не локальной базы скрипт сначала покажет, куда подключается, и попросит подтверждение.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import models  # noqa: E402
from core.config import settings  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def db_url() -> str:
    url = settings.database_url
    # Render отдаёт postgres://, а драйверу нужен postgresql+asyncpg://
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def list_admins(db):
    rows = (await db.execute(
        select(models.User).where(models.User.is_platform_admin.is_(True)).order_by(models.User.id),
    )).scalars().all()
    if not rows:
        print("Админов платформы нет")
    for u in rows:
        print(f"{u.id:>4}  {u.email}")


async def set_admin(db, email: str, value: bool):
    user = (await db.execute(
        select(models.User).where(func.lower(models.User.email) == email.strip().lower()),
    )).scalars().first()
    if not user:
        raise SystemExit(f"Пользователь {email} не найден")
    if user.is_platform_admin == value:
        print(f"У {user.email} уже {'есть' if value else 'нет'} прав админа, менять нечего")
        return
    user.is_platform_admin = value
    await db.commit()
    print(f"{user.email}: {'теперь админ платформы' if value else 'права админа сняты'}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--email")
    parser.add_argument("--revoke", action="store_true", help="снять права вместо выдачи")
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение для не локальной базы")
    args = parser.parse_args()
    if not args.list and not args.email:
        parser.error("нужно либо --list, либо --email")

    url = make_url(db_url())
    if url.host not in LOCAL_HOSTS and not args.yes:
        print(f"База НЕ локальная: {url.host}/{url.database}")
        if input("Продолжить? (y/N) ").strip().lower() != "y":
            raise SystemExit("Отменено")

    engine = create_async_engine(url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await (list_admins(db) if args.list else set_admin(db, args.email, not args.revoke))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
