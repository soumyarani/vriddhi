from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # ---- App ----
    app_name: str = "WhatsApp Commerce"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    api_base_url: str = "http://localhost:8000"
    storefront_url: str = "http://localhost:3000"
    log_level: str = "INFO"

    # ---- Database ----
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/whatsapp_commerce"
    db_pool_min: int = 5
    db_pool_max: int = 20
    db_pool_recycle_seconds: int = 1800
    db_echo: bool = False

    # ---- Redis ----
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = 50

    # ---- JWT ----
    jwt_secret: str = "change-me-in-production-please-use-a-long-random-string"
    jwt_secret_previous: str | None = None
    jwt_key_id: str = "k1"
    jwt_key_id_previous: str | None = None
    jwt_algorithm: str = "HS256"
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7

    # ---- Google OAuth ----
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:3000/auth/callback"
    agent_allowed_domains: str = ""

    # ---- WhatsApp / Meta ----
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_catalog_id: str = ""
    whatsapp_business_id: str = ""
    meta_graph_version: str = "v21.0"
    meta_graph_url: str = "https://graph.facebook.com"

    # ---- OpenAI ----
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_timeout_seconds: float = 25.0
    ai_max_context_tokens: int = 3000
    ai_summary_token_budget: int = 200
    ai_circuit_breaker_threshold: int = 3
    ai_circuit_breaker_cooldown_seconds: int = 60

    # ---- Cashfree ----
    cashfree_app_id: str = ""
    cashfree_secret_key: str = ""
    cashfree_env: Literal["sandbox", "production"] = "sandbox"
    cashfree_api_version: str = "2023-08-01"
    cashfree_webhook_secret: str = ""
    payment_link_ttl_minutes: int = 30

    # ---- Conversations ----
    agent_sla_minutes: int = 15
    conversation_reopen_hours: int = 24

    # ---- Commerce rules ----
    currency: str = "INR"
    cart_ttl_hours: int = 24
    reservation_ttl_minutes: int = 35
    return_window_days: int = 7
    abandoned_cart_after_hours: int = 2
    default_gst_rate: float = 18.0
    seller_gstin: str = ""
    seller_legal_name: str = "WhatsApp Commerce Pvt Ltd"
    seller_address: str = "Bengaluru, Karnataka, India"
    seller_state_code: str = "29"

    # ---- Security ----
    cors_allowed_origins: str = "http://localhost:3000"
    max_request_body_bytes: int = 1 * 1024 * 1024
    max_webhook_body_bytes: int = 5 * 1024 * 1024
    webhook_ip_whitelist_enabled: bool = False
    meta_webhook_ips: str = ""
    cashfree_webhook_ips: str = ""
    enable_hsts: bool = True

    # ---- Rate limits (requests per minute) ----
    rate_limit_auth: str = "10/minute"
    rate_limit_coupon: str = "5/minute"
    rate_limit_store: str = "60/minute"
    rate_limit_admin: str = "120/minute"
    rate_limit_webhook: str = "1000/minute"
    rate_limit_enabled: bool = True

    # ---- Email ----
    email_enabled: bool = False
    email_from: str = "orders@example.com"
    email_from_name: str = "WhatsApp Commerce"
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True

    # ---- Pagination ----
    default_page_size: int = 20
    max_page_size: int = 100

    @field_validator("agent_allowed_domains", "cors_allowed_origins", mode="before")
    @classmethod
    def _strip(cls, v: object) -> object:
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _guard_production(self) -> "Settings":
        """Refuse to boot in production with development placeholders.

        These are all things that work perfectly in staging and are a breach in
        production, which is exactly the combination that reaches deploy day
        unnoticed. Failing at import is loud and cheap; the alternative is a
        service that starts happily and signs tokens with a secret published in
        the example file.

        Only enforced when ENVIRONMENT=production, so development and the test
        suite are unaffected.
        """
        if self.environment != "production":
            return self

        problems: list[str] = []

        if self.debug:
            problems.append("DEBUG must be false (it exposes tracebacks to callers)")

        # The placeholder is committed in .env.example, so treating it as valid
        # would mean anyone with the repo can mint tokens.
        if "CHANGE_ME" in self.jwt_secret:
            problems.append("JWT_SECRET is still the .env.example placeholder")
        elif len(self.jwt_secret) < 32:
            problems.append(
                f"JWT_SECRET is {len(self.jwt_secret)} chars; use at least 32 "
                '(python -c "import secrets; print(secrets.token_urlsafe(64))")'
            )

        origins = [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]
        if "*" in origins:
            problems.append("CORS_ALLOWED_ORIGINS must not be '*' in production")
        if not origins:
            problems.append("CORS_ALLOWED_ORIGINS must list the storefront origin")

        # Without these the signature checks cannot distinguish a real callback
        # from a forged one, which is the whole basis of trusting a webhook.
        for name, value in (
            ("WHATSAPP_APP_SECRET", self.whatsapp_app_secret),
            ("WHATSAPP_VERIFY_TOKEN", self.whatsapp_verify_token),
            ("CASHFREE_WEBHOOK_SECRET", self.cashfree_webhook_secret),
        ):
            if not value or "CHANGE_ME" in value:
                problems.append(f"{name} must be set (webhook signatures depend on it)")

        if problems:
            raise ValueError(
                "Refusing to start in production:\n  - " + "\n  - ".join(problems)
            )
        return self

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def agent_domains(self) -> list[str]:
        return [d.strip().lower().lstrip("@") for d in self.agent_allowed_domains.split(",") if d.strip()]

    @property
    def meta_ip_list(self) -> list[str]:
        return [i.strip() for i in self.meta_webhook_ips.split(",") if i.strip()]

    @property
    def cashfree_ip_list(self) -> list[str]:
        return [i.strip() for i in self.cashfree_webhook_ips.split(",") if i.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def cashfree_base_url(self) -> str:
        return (
            "https://api.cashfree.com/pg"
            if self.cashfree_env == "production"
            else "https://sandbox.cashfree.com/pg"
        )

    @property
    def graph_base(self) -> str:
        return f"{self.meta_graph_url}/{self.meta_graph_version}"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
