from fastapi import HTTPException, status

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import re


async def get_owned(db: AsyncSession, model, obj_id: int, owner_id: int, name: str):
    result = await db.execute(
        select(model).where(model.id == obj_id, model.owner_id == owner_id),
    )
    obj = result.scalars().first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} not found")
    return obj


def slugify(text: str) -> str:
    slug = text.lower().strip()
    slug = re.sub(r"[^a-z0-9\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    return slug.strip("-") or "salon"


async def generate_unique_slug(db: AsyncSession, model, base_text: str) -> str:
    base_slug = slugify(base_text)
    slug = base_slug
    counter = 1
    while True:
        result = await db.execute(select(model).where(model.slug == slug))
        if not result.scalars().first():
            return slug
        slug = f"{base_slug}-{counter}"
        counter += 1