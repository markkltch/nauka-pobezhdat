"""Settings, read from the environment (.env on the server)."""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # site
    base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"
    admin_emails: str = ""  # comma-separated
    database_url: str = "sqlite:///./dev.db"

    # access
    free_beta: bool = True  # everyone gets access without payment until billing is live
    monthly_plan_limit: int = 30  # разборов (plans) per person per month
    messages_per_situation: int = 24  # hard cap on messages in one conversation
    price_per_seat_rub: int = 1500

    # mail (SMTP). Empty host = codes and reminders go to the log (development)
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "Наука побеждать <no-reply@example.com>"
    smtp_ssl: bool = True

    # LLM: "gigachat", "yandex" or "fake"
    llm_provider: str = "fake"
    llm_timeout_s: float = 60.0
    # GigaChat
    gigachat_auth_key: str = ""  # Basic authorization key from the Sber developers portal
    gigachat_scope: str = "GIGACHAT_API_B2B"
    gigachat_light_model: str = "GigaChat-2"
    gigachat_strong_model: str = "GigaChat-2-Pro"
    gigachat_ca_bundle: str = ""  # path to the Russian Trusted Root CA bundle; empty = system store
    # YandexGPT
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_light_model: str = "yandexgpt-lite/latest"
    yandex_strong_model: str = "yandexgpt/latest"
    # prices, rub per 1000 tokens incl. VAT (for the cost ledger)
    price_light_per_1k: float = 0.065
    price_strong_per_1k: float = 0.5

    # YooKassa
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_vat_code: int = 1  # 1 = без НДС (УСН); change when VAT applies

    @property
    def admins(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def billing_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
