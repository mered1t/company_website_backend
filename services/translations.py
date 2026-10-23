"""Переводы услуг: показ на языке посетителя и черновик перевода через ИИ.

Черновик перевода стоит денег, поэтому:
  * переводятся только запрошенные языки (target_languages);
  * готовые переводы кэшируются (translation_cache, общий для всех салонов): одинаковый текст в ИИ не уходит;
  * лимит — 30 обращений к ИИ в час на салон; ответ целиком из кэша лимит не тратит.
"""
import hashlib
import json
import math
import time

from fastapi import HTTPException
from limits import parse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

import models
from core.config import settings
from core.rate_limiter import limiter
from services.i18n import SUPPORTED_LANGUAGES

TRANSLATE_LIMIT = parse("30/hour")  # обращений к ИИ на один салон

LANGUAGE_NAMES = {"en": "English", "pl": "Polish", "es": "Spanish", "uk": "Ukrainian", "sq": "Albanian"}


def localize(item, translations: dict | None, lang: str | None):
    """item — схема ответа с name/description (меняется на месте). Нет перевода — остаётся исходный текст."""
    if not lang:
        return item
    t = (translations or {}).get(lang)
    if t and t.get("name"):
        item.name = t["name"]
        if t.get("description"):
            item.description = t["description"]
    return item


PROMPT = """You translate service names and descriptions for a salon/studio booking page.
Translate the JSON input "name" and "description" into each language listed in "targets".
Keep it short and natural, as a local salon would write it. Keep brand names, numbers and units as they are.
Answer ONLY with JSON: {"<language code>": {"name": "...", "description": "..." or null}, ...}"""


async def call_translation_model(payload: dict) -> dict:
    """Единственное место, где перевод ходит в OpenAI (в тестах подменяется)."""
    if not settings.openai_api_key or not settings.openai_model:
        raise HTTPException(status_code=503, detail="AI service is not configured")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=60.0, max_retries=1)
    response = await client.responses.create(
        model=settings.openai_model, instructions=PROMPT, input=json.dumps(payload, ensure_ascii=False),
        max_output_tokens=2000, store=False,
    )
    return json.loads((response.output_text or "").strip().removeprefix("```json").removesuffix("```").strip())


async def draft_translations(name: str, description: str | None, source: str | None,
                             targets: list[str] | None) -> dict:
    """{lang: {name, description}} для запрошенных языков. Лишнее и пустое из ответа модели отбрасывается."""
    targets = [lang for lang in (targets or SUPPORTED_LANGUAGES) if lang != source]
    if not targets:
        return {}
    payload = {"name": name, "description": description, "source_language": LANGUAGE_NAMES.get(source or ""),
               "targets": {lang: LANGUAGE_NAMES[lang] for lang in targets}}
    try:
        raw = await call_translation_model(payload)
    except HTTPException:
        raise
    except Exception as error:  # noqa: BLE001 — сбой модели или неразборчивый ответ
        raise HTTPException(status_code=502, detail="Translation failed, please try again") from error
    result = {}
    for lang in targets:
        item = raw.get(lang) if isinstance(raw, dict) else None
        if isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip():
            desc = item.get("description")
            result[lang] = {"name": item["name"].strip()[:150],
                            "description": desc.strip()[:2000] if isinstance(desc, str) and desc.strip() else None}
    return result


# ------------------------------------------------------------------ кэш и лимит
def _normalize(text: str | None) -> str:
    return " ".join((text or "").split())


def source_hash(name: str, description: str | None) -> str:
    """Ключ кэша: тот же текст с другими пробелами — тот же ключ. Регистр важен («стрижка» и «Стрижка» разные)."""
    return hashlib.sha256(f"{_normalize(name)}\x00{_normalize(description)}".encode()).hexdigest()


async def cached_translations(db: AsyncSession, key: str, languages: list[str]) -> dict:
    rows = (await db.execute(
        select(models.TranslationCache)
        .where(models.TranslationCache.source_hash == key, models.TranslationCache.language.in_(languages)),
    )).scalars().all()
    return {r.language: {"name": r.name, "description": r.description} for r in rows}


async def store_translations(db: AsyncSession, key: str, translations: dict) -> None:
    """Сохраняет новые переводы в кэш (без коммита). Если такой уже есть (параллельный запрос), он остаётся."""
    if not translations:
        return
    await db.execute(
        insert(models.TranslationCache)
        .values([{"source_hash": key, "language": lang, "name": t["name"], "description": t["description"]}
                 for lang, t in translations.items()])
        .on_conflict_do_nothing(constraint="uq_translation_cache_source_language"),
    )


def quota(organization_id: int) -> tuple[int, int]:
    """(сколько обращений к ИИ осталось в этом часе, через сколько секунд счётчик обнулится)."""
    reset_at, remaining = limiter.limiter.get_window_stats(TRANSLATE_LIMIT, "translate", str(organization_id))
    return remaining, max(math.ceil(reset_at - time.time()), 1)


def spend_quota(organization_id: int) -> None:
    """Списывает одно обращение к ИИ. Лимит исчерпан — 429 с Retry-After."""
    if not limiter.enabled:
        return
    if not limiter.limiter.hit(TRANSLATE_LIMIT, "translate", str(organization_id)):
        _, retry_after = quota(organization_id)
        raise HTTPException(
            status_code=429,
            detail="Translation limit reached: 30 AI translations per hour. Please try again later.",
            headers={"Retry-After": str(retry_after), "X-RateLimit-Limit": str(TRANSLATE_LIMIT.amount),
                     "X-RateLimit-Remaining": "0"},
        )
