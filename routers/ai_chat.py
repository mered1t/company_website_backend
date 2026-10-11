"""ИИ-чат по аналитике салона. Подключён только к /api/v1, доступ у владельца и админов (ИИ: пробный период и тариф pro)."""
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

import services.ai_chat as ai_chat
from auth.auth import ManagerMembership
from core.rate_limiter import limiter
from db.database import get_db
from schemas.ai_chat import AiChatRequest, AiChatResponse, AiChatUsage

router = APIRouter()

DB = Annotated[AsyncSession, Depends(get_db)]


@router.get("/usage", response_model=AiChatUsage)
async def get_chat_usage(db: DB, membership: ManagerMembership):
    return await ai_chat.get_usage(db, membership.organization_id)


@router.post("", response_model=AiChatResponse)
@limiter.limit("10/minute")
async def ask(request: Request, db: DB, membership: ManagerMembership, payload: AiChatRequest):
    return await ai_chat.answer(db, membership, payload.messages, payload.language)
