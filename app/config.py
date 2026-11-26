"""Settings, read from the environment (.env on the server)."""
from functools import lru_cache
from typing import Literal

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
    budget_per_situation_rub: float = 4.4  # hard cap on model spend for one разбор (estimated before each call)
    messages_per_situation: int = 24  # hard cap on messages in one conversation
    price_per_seat_rub: int = 1500
    pack_size: int = 10  # extra разборов in one pack, for paid users
    pack_price_rub: int = 590
    voice_per_month: int = 30  # голосовых в месяц на полном тарифе
    voice_free_per_month: int = 5
    voice_max_seconds: int = 30
    price_voice_per_15s: float = 0.2  # SpeechKit, ₽ за 15 с, оценка с запасом

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
    chars_per_token_estimate: float = 3.2  # measured ≈3.8 for Russian on Alice AI  # conservative, for the budget check before a call
    yandex_speech_api_key: str = ""  # ключ с областью yc.ai.speechkitStt.execute; пусто — берём yandex_api_key
    yandex_oauth_client_id: str = ""  # вход через Яндекс ID
    yandex_oauth_client_secret: str = ""
    max_out_questions: int = 400
    max_out_plan: int = 1400
    max_out_talk: int = 600

    # Choose which provider handles new orders. Existing YooKassa orders keep their credentials.
    payment_provider: Literal["yookassa", "tbank"] = "yookassa"
    personal_data_operator: Literal["ogi", "ip"] = "ogi"
    merchant_email: str = ""
    merchant_phone: str = ""
    merchant_postal_address: str = ""
    merchant_rkn_number: str = ""

    tbank_terminal_key: str = ""
    tbank_password: str = ""
    tbank_taxation: Literal["", "osn", "usn_income", "usn_income_outcome", "esn", "patent"] = ""
    tbank_vat: str = ""
    tbank_ffd: Literal["1.05", "1.2"] = "1.05"

    # YooKassa
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    yookassa_vat_code: int = 1  # 1 = без НДС. У ОГИ ОСНО (есть филиал): если НДС не освобождён, нужна ставка 22% — код по справочнику ЮKassa

    @property
    def admins(self) -> set[str]:
        return {e.strip().lower() for e in self.admin_emails.split(",") if e.strip()}

    @property
    def billing_enabled(self) -> bool:
        if self.payment_provider == "tbank":
            return bool(
                self.tbank_credentials and self.tbank_taxation
                and self.tbank_vat in {"none", "vat0", "vat5", "vat7", "vat10", "vat22"}
                and self.base_url.startswith("https://")
            )
        return self.yookassa_enabled

    @property
    def tbank_credentials(self) -> bool:
        return bool(self.tbank_terminal_key and self.tbank_password)

    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def checkout_enabled(self) -> bool:
        return self.billing_enabled and not self.free_beta


@lru_cache
def get_settings() -> Settings:
    return Settings()
