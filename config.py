from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    secret_key: SecretStr
    algorithm: str = "HS256"
    database_url: str
    access_token_expire_minutes: int = 30
    resend_api_key: str
    sentry_dsn: str | None = None
    environment: str = "production"
    email_from: str = "onboarding@resend.dev"
    email_reply_to: str | None = None
    frontend_url: str = "https://koracrm.com"
    app_name: str = "Твоё название CRM"

    # ИИ-аналитика (модель и ключ задаются в переменных окружения)
    openai_api_key: str | None = None
    openai_model: str | None = None
    ai_monthly_limit: int = 30

    # за сколько часов до начала клиент ещё может сам отменить или перенести запись
    booking_change_cutoff_hours: int = 2

settings = Settings() #type: ignore[call-arg] # Loaded from env file