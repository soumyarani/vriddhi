from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Vriddhi Commerce API"
    database_url: str = "sqlite:///./vriddhi.db"
    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 30
    max_request_body_bytes: int = 1_000_000
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    redis_url: str | None = None


settings = Settings()
