from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parents[1] / ".env",
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
    # публичный адрес этого бэкенда (например https://api.clientum.app). Если задан, ссылка «подтвердить почту»
    # в письме ведёт на страницу бэкенда и работает без отдельной страницы на фронтенде
    backend_url: str | None = None
    # дополнительные адреса фронтенда для CORS через запятую (адрес FRONTEND_URL разрешён всегда)
    cors_origins: str = "http://localhost:4200"
    app_name: str = "Твоё название CRM"

    # Swagger (/docs, /redoc, /openapi.json). В production открывается только по логину и паролю (HTTP Basic);
    # если пароль не задан, документация в production выключена. Локально и в тестах открыта.
    docs_username: str = "admin"
    docs_password: SecretStr | None = None

    # ИИ-аналитика (модель и ключ задаются в переменных окружения)
    openai_api_key: str | None = None
    openai_model: str | None = None
    ai_monthly_limit: int = 4          # ИИ-отчётов в месяц на салон: хватает на понедельные или помесячные
    ai_chat_monthly_limit: int = 30    # вопросов ИИ-чату в месяц на салон (счётчик отдельный от отчётов)

    # за сколько часов до начала клиент ещё может сам отменить или перенести запись
    booking_change_cutoff_hours: int = 2

    # напоминания о записи
    reminder_hours_before: int = 24                # за сколько часов до начала отправлять
    reminders_in_app: bool = False                 # True: проверка идёт внутри веб-сервиса (REMINDERS_IN_APP=1)
    reminder_check_interval_seconds: int = 900     # как часто проверять (15 минут)

    # чистка базы (раз в сутки, в том же фоновом цикле): журнал действий старше стольких дней удаляется; 0 — хранить всё
    activity_log_retention_days: int = 365

settings = Settings() #type: ignore[call-arg] # Loaded from env file