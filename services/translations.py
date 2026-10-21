"""Переводы услуг: показ на языке посетителя и черновик перевода через ИИ."""
import json

from fastapi import HTTPException

from core.config import settings
from services.i18n import SUPPORTED_LANGUAGES

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
