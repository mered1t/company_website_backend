"""Меняет тариф организации и показывает список организаций.

Запуск (из корня проекта):
    python3 scripts/set_plan.py --list                    # все организации и их тарифы
    python3 scripts/set_plan.py --org-id 1 --plan pro     # включить pro (ИИ-аналитика)
    python3 scripts/set_plan.py --org-id 1 --plan basic   # вернуть basic

База берётся из DATABASE_URL (.env или переменная окружения). Чтобы поменять тариф на Render,
запусти с адресом боевой базы:  DATABASE_URL="postgresql+asyncpg://..." python3 scripts/set_plan.py ...
Для не локальной базы скрипт сначала покажет, куда подключается, и попросит подтверждение.
"""
import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import models  # noqa: E402
from core.config import settings  # noqa: E402
from domain.plans import Plan  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}


def db_url() -> str:
    url = settings.database_url
    # Render отдаёт postgres://, а драйверу нужен postgresql+asyncpg://
    if url.startswith("postgres://"):
        url = "postgresql+asyncpg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+asyncpg://" + url[len("postgresql://"):]
    return url


async def list_orgs(db):
    rows = (await db.execute(select(models.Organization).order_by(models.Organization.id))).scalars().all()
    if not rows:
        print("Организаций нет")
    for o in rows:
        print(f"{o.id:>4}  {o.plan:<6}  {o.name}")


async def set_plan(db, org_id: int, plan: str):
    org = await db.get(models.Organization, org_id)
    if not org:
        raise SystemExit(f"Организация {org_id} не найдена")
    if org.plan == plan:
        print(f"У организации {org_id} ({org.name}) уже тариф {plan}, менять нечего")
        return
    old = org.plan
    org.plan = plan
    await db.commit()
    print(f"Организация {org_id} ({org.name}): {old} -> {plan}")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--org-id", type=int)
    parser.add_argument("--plan", choices=[p.value for p in Plan])
    parser.add_argument("--yes", action="store_true", help="не спрашивать подтверждение для не локальной базы")
    args = parser.parse_args()
    if not args.list and not (args.org_id and args.plan):
        parser.error("нужно либо --list, либо --org-id и --plan вместе")

    url = make_url(db_url())
    if url.host not in LOCAL_HOSTS and not args.yes:
        print(f"База НЕ локальная: {url.host}/{url.database}")
        if input("Продолжить? (y/N) ").strip().lower() != "y":
            raise SystemExit("Отменено")

    engine = create_async_engine(url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await (list_orgs(db) if args.list else set_plan(db, args.org_id, args.plan))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())