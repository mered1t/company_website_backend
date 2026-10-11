from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from services.i18n import Language

MAX_MESSAGES = 20          # сколько последних сообщений переписки фронтенд может прислать
MAX_MESSAGE_LENGTH = 2000  # символов в одном сообщении


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]  # "system" и прочие роли запрещены: инструкции модели задаёт только сервер
    content: str = Field(max_length=MAX_MESSAGE_LENGTH)

    @field_validator("content")
    @classmethod
    def not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Message must not be empty")
        return v


class AiChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=MAX_MESSAGES)
    language: Language | None = None  # запасной язык ответа; обычно модель отвечает на языке вопроса

    @model_validator(mode="after")
    def ends_with_question(self) -> "AiChatRequest":
        if self.messages[-1].role != "user":
            raise ValueError("The last message must be from the user")
        return self


class AiChatResponse(BaseModel):
    reply: str
    used_this_month: int
    monthly_limit: int


class AiChatUsage(BaseModel):
    ai_enabled: bool
    used_this_month: int
    monthly_limit: int
