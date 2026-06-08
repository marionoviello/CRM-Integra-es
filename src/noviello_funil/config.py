"""Typed environment-backed settings.

All secrets and tunables come from environment variables (or `.env` file
during development). Never hardcode values that vary across environments.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # External APIs
    anthropic_api_key: str
    anthropic_model: str = "claude-sonnet-4-5"
    jurichat_api_key: str
    jurichat_webhook_secret: str
    jurichat_base_url: str = "https://api.jurichat.com"

    # Mario's WhatsApp number for notifications (E.164, digits only)
    notificacao_telefone: str

    # Mario's conversation ID inside Jurichat (the conversation the bot
    # sends Mario's notifications to).
    mario_conversation_id: str

    # SQLite
    database_path: str = "./data/noviello.db"

    # FastAPI
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    log_level: str = "INFO"

    # Throttling & limits
    max_turnos_por_lead: int = 20
    throttle_msg_por_segundo: float = 1.0

    # Follow-up timers (horas)
    followup_1_apos_horas: int = Field(default=48, ge=1)
    followup_2_apos_horas: int = Field(default=72, ge=1)
    encerramento_apos_horas: int = Field(default=24, ge=1)
