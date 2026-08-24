from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def get_owned(db: AsyncSession, model, obj_id: int, owner_id: int, name: str):
    result = await db.execute(
        select(model).where(model.id == obj_id, model.owner_id == owner_id),
    )
    obj = result.scalars().first()
    if not obj:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"{name} not found")
    return obj