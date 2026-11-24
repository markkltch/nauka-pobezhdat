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
    run_jobs_in_process: bool = True  # false on shared hosting: cron runs app.cron instead

    # access
    free_beta: bool = True  # everyone gets the full plan without payment until billing is live
    monthly_plan_limit: int = 30  # разборов per paid person per month
    free_plans_per_month: int = 3  # разборов per month on the free plan
    budget_per_situation_rub: float = 4.8  # hard cap on model spend for one разбор (estimated before each call)
    messages_per_situation: int = 24  # hard cap on messages in one conversation
    price_per_seat_rub: int = 1500
    pack_size: int = 10  # extra разборов in one pack, for paid users
    pack_price_rub: int = 590

    # mail (SMTP). Empty host = codes and reminders go to the log (development)
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "Наука побеждать <no-reply@example.com>"
    smtp_ssl: bool = True
    mail_via_sendmail: bool = False  # true on shared hosting: use the local sendmail
    sendmail_path: str = "/usr/sbin/sendmail"

    # LLM: "gigachat", "yandex" or "fake"
    llm_provider: str = "fake"  # production: "yandex"
    llm_timeout_s: float = 60.0
    # GigaChat
    gigachat_auth_key: str = ""  # Basic authorization key from the Sber developers portal
    gigachat_scope: str = "GIGACHAT_API_B2B"
    gigachat_light_model: str = "GigaChat-2"
    gigachat_strong_model: str = "GigaChat-2-Pro"
    gigachat_ca_bundle: str = ""  # path to the Russian Trusted Root CA bundle; empty = system store
    # Yandex AI Studio (OpenAI-compatible Chat Completions)
    yandex_api_key: str = ""
    yandex_folder_id: str = ""
    yandex_base_url: str = "https://ai.api.cloud.yandex.net/v1"
    yandex_light_model: str = "aliceai-llm-flash/latest"
    yandex_strong_model: str = "aliceai-llm/latest"
    # prices, rub per 1000 tokens incl. VAT; defaults = Yandex AI Studio, November 2026
    price_light_in_per_1k: float = 0.1
    price_light_out_per_1k: float = 0.2
    price_strong_in_per_1k: float = 0.5
    price_strong_out_per_1k: float = 1.2
    chars_per_token_estimate: float = 2.5  # conservative, for the budget check before a call
    max_out_questions: int = 400
    max_out_plan: int = 1200
    max_out_talk: int = 600

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
