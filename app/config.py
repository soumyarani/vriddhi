from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Vriddhi Commerce API"
    environment: str = "development"

    database_url: str = "sqlite:///./vriddhi.db"
    redis_url: str | None = None

    jwt_secret_key: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "vriddhi"
    access_token_expire_minutes: int = 15
    refresh_token_expire_days: int = 7

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    agent_allowed_domains: str = ""

    max_request_body_bytes: int = 1_000_000
    max_webhook_body_bytes: int = 5_000_000

    cors_allowed_origins: str = "http://localhost:3000"

    whatsapp_app_secret: str = ""
    webhook_ip_whitelist: str = ""
    cashfree_webhook_secret: str = ""
    payment_link_ttl_minutes: int = 30

    store_rate_limit_per_minute: int = 60
    admin_rate_limit_per_minute: int = 120
    webhook_rate_limit_per_minute: int = 1000
    coupon_rate_limit_per_minute: int = 5

    @property
    def cors_origins(self) -> list[str]:
        return [v.strip() for v in self.cors_allowed_origins.split(",") if v.strip()]

    @property
    def agent_domains(self) -> set[str]:
        return {v.strip().lower() for v in self.agent_allowed_domains.split(",") if v.strip()}

    @property
    def webhook_ip_set(self) -> set[str]:
        return {v.strip() for v in self.webhook_ip_whitelist.split(",") if v.strip()}


settings = Settings()
