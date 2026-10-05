"""Наполняет ЛОКАЛЬНУЮ базу демо-данными для организации: мастера, услуги, клиенты и ~90 дней записей.

Запуск (из корня проекта):
    python3 scripts/seed_demo.py --org-id 1          # добавить демо-данные
    python3 scripts/seed_demo.py --org-id 1 --clean  # убрать демо-данные

Скрипт откажется работать, если база не локальная (чтобы случайно не засорить прод).
Демо-клиенты помечены телефонами +99900000xxx, демо-мастера и услуги -- именами из списков ниже.
"""
import argparse
import asyncio
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import delete, select  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import models  # noqa: E402
from config import settings  # noqa: E402

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres"}
PHONE_PREFIX = "+999000"

SERVICES = [  # (название, цена в минимальных единицах, минут, популярность)
    ("Men's haircut", 2500, 45, 10),
    ("Beard trim", 1500, 30, 6),
    ("Haircut + beard", 3800, 60, 5),
    ("Kids haircut", 1800, 30, 3),
    ("Styling", 2000, 30, 2),
    ("Hair coloring", 6500, 60, 1),
]
MASTERS = [  # (имя, популярность, вероятность отмены)
    ("Alex Stone", 10, 0.06),
    ("Marta Nowak", 8, 0.10),
    ("Piotr Zielinski", 5, 0.12),
    ("Ina Koval", 3, 0.28),  # у неё часто отменяют
]
FIRST = ["Adam", "Jan", "Marek", "Oleh", "Taras", "Leon", "Mateo", "Luka", "Arben", "Besnik", "Dmytro", "Andrii",
         "Pawel", "Kacper", "Diego", "Carlos", "Erion", "Ilir", "Maksym", "Bohdan"]
LAST = ["Kowal", "Shevchenko", "Garcia", "Hoxha", "Nowicki", "Bondar", "Lopez", "Dervishi", "Wojcik", "Melnyk"]
HOURS_WEIGHT = {10: 3, 11: 5, 12: 6, 13: 4, 14: 3, 15: 4, 16: 5, 17: 7, 18: 6}
WEEKDAY_WEIGHT = {0: 0.35, 1: 0.6, 2: 0.65, 3: 0.7, 4: 0.9, 5: 1.0, 6: 0.0}  # вс выходной


def check_local():
    host = make_url(settings.database_url).host
    if host not in LOCAL_HOSTS:
        raise SystemExit(f"Отказ: база на хосте {host!r}, скрипт работает только с локальной базой")


async def clean(db, org_id: int):
    demo_clients = (await db.execute(
        select(models.Client.id).where(models.Client.organization_id == org_id, models.Client.phone.like(PHONE_PREFIX + "%")),
    )).scalars().all()
    if demo_clients:
        await db.execute(delete(models.Appointment).where(models.Appointment.client_id.in_(demo_clients)))
        await db.execute(delete(models.Client).where(models.Client.id.in_(demo_clients)))
    for model, names in ((models.Service, [s[0] for s in SERVICES]), (models.Master, [m[0] for m in MASTERS])):
        rows = (await db.execute(select(model).where(model.organization_id == org_id, model_name(model).in_(names)))).scalars().all()
        for row in rows:
            fk = models.Appointment.service_id if model is models.Service else models.Appointment.master_id
            used = (await db.execute(select(models.Appointment.id).where(fk == row.id).limit(1))).first()
            if used:
                continue
            # удаляем запросами, а не через ORM: у мастера есть связанные таблицы, а ленивая загрузка в async недоступна
            if model is models.Service:
                await db.execute(delete(models.MasterService).where(models.MasterService.service_id == row.id))
            else:
                for child in (models.MasterService, models.WorkingHours, models.WorkingHoursException, models.TimeOff):
                    await db.execute(delete(child).where(child.master_id == row.id))
            await db.execute(delete(model).where(model.id == row.id))
    await db.commit()
    print("Демо-данные удалены")


def model_name(model):
    return model.name if model is models.Service else model.full_name


async def seed(db, org_id: int):
    org = await db.get(models.Organization, org_id)
    if not org:
        raise SystemExit(f"Организация {org_id} не найдена")
    exists = (await db.execute(
        select(models.Client.id).where(models.Client.organization_id == org_id, models.Client.phone.like(PHONE_PREFIX + "%")).limit(1),
    )).first()
    if exists:
        raise SystemExit("Демо-данные уже есть. Сначала запусти с --clean")

    rnd = random.Random(42)
    services = [models.Service(organization_id=org_id, name=n, price=p, duration_minutes=d) for n, p, d, _ in SERVICES]
    masters = [models.Master(organization_id=org_id, full_name=n) for n, _, _ in MASTERS]
    clients = []
    for i in range(60):
        clients.append(models.Client(
            organization_id=org_id,
            full_name=f"{rnd.choice(FIRST)} {rnd.choice(LAST)}",
            phone=f"{PHONE_PREFIX}{i:05d}",
        ))
    db.add_all(services + masters + clients)
    await db.flush()
    db.add_all([models.MasterService(master_id=m.id, service_id=s.id) for m in masters for s in services])
    # рабочие часы пн-сб 10:00-19:00, чтобы считалась загрузка мастеров
    db.add_all([
        models.WorkingHours(master_id=m.id, day_of_week=d, start_time="10:00", end_time="19:00")
        for m in masters for d in range(6)
    ])

    # типы клиентов: постоянные ходят часто, обычные редко, «ушедшие» не были 60+ дней, новые появляются недавно
    loyal, regular, churned, fresh = clients[:12], clients[12:36], clients[36:50], clients[50:]
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    start_day = today - timedelta(days=90)
    first_seen = {c.id: start_day for c in clients}
    for c in fresh:
        first_seen[c.id] = today - timedelta(days=rnd.randint(3, 25))

    def pick_client(day):
        pool = []
        for c in loyal:
            pool += [c] * 6
        for c in regular:
            pool += [c] * 2
        if day < today - timedelta(days=60):
            pool += churned * 3
        pool += [c for c in fresh if first_seen[c.id] <= day] * 3
        return rnd.choice(pool)

    total = 0
    for offset in range(91 + 7):
        day = start_day + timedelta(days=offset)
        weight = WEEKDAY_WEIGHT[day.weekday()]
        if weight == 0:
            continue
        busy = set()  # один визит в день на клиента
        for master, (_, m_pop, cancel_p) in zip(masters, MASTERS):
            for hour, h_weight in HOURS_WEIGHT.items():
                if rnd.random() > weight * (m_pop / 10) * (h_weight / 7) * 1.1:
                    continue
                service = rnd.choices(services, weights=[s[3] for s in SERVICES])[0]
                client = pick_client(day)
                if client.id in busy:
                    continue
                busy.add(client.id)
                start = day.replace(hour=hour, minute=rnd.choice([0, 0, 30]) if service.duration_minutes <= 30 else 0)
                end = start + timedelta(minutes=service.duration_minutes)
                if day > today:
                    status = "cancelled" if rnd.random() < cancel_p / 2 else "scheduled"
                elif day == today:
                    status = "scheduled"
                else:
                    status = "cancelled" if rnd.random() < cancel_p else "completed"
                db.add(models.Appointment(
                    organization_id=org_id, client_id=client.id, service_id=service.id, master_id=master.id,
                    start_time=start, end_time=end, status=status, price=service.price, currency=org.currency,
                ))
                total += 1
    await db.commit()
    print(f"Создано: {len(masters)} мастеров, {len(services)} услуг, {len(clients)} клиентов, {total} записей")


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--org-id", type=int, required=True)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    check_local()
    engine = create_async_engine(settings.database_url)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        await (clean(db, args.org_id) if args.clean else seed(db, args.org_id))
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())