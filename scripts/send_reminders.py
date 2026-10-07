"""Разовая отправка напоминаний о записи. Для Render Cron Job (каждые 15 минут: */15 * * * *).

Запуск (из корня проекта):
    python3 scripts/send_reminders.py

Нужен, только если REMINDERS_IN_APP не включён: тогда проверку делает сам веб-сервис.
Если включить оба способа сразу, ничего страшного: письмо не уйдёт дважды.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import AsyncSessionLocal, engine  # noqa: E402
from services.reminders import send_due_reminders  # noqa: E402


async def main() -> None:
    async with AsyncSessionLocal() as db:
        count = await send_due_reminders(db)
    await engine.dispose()
    print(f"Отправлено напоминаний: {count}")


if __name__ == "__main__":
    asyncio.run(main())
