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
    access_token_expire_minutes: int = 60 * 24
    resend_api_key: str

settings = Settings() #type: ignore[call-arg] # Loaded from env file