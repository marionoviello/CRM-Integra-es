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

    # ID da inbox do Jurichat que o bot atende. Obrigatório no endpoint
    # GET /conversation (sem ele, 400 Validation error). Aparece no
    # payload de qualquer webhook como ``data.inboxId``.
    jurichat_inbox_id: str

    # Mario's WhatsApp number for notifications (E.164, digits only)
    notificacao_telefone: str

    # Mario's conversation ID inside Jurichat (the conversation the bot
    # sends Mario's notifications to). Aceita MÚLTIPLOS ids separados
    # por vírgula ("id_mario,id_equipe") — todos recebem as notificações
    # e todos ficam protegidos de virar lead (2026-06-12).
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

    # Google Calendar — agendamento de reuniões com Mario via WhatsApp.
    # Setup one-time: ``scripts/google_oauth_setup.py`` gera o refresh_token
    # a partir do client_id/secret (OAuth Desktop app no GCP). O refresh
    # token não expira (a menos que Mario revogue manualmente).
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_refresh_token: str = ""
    google_calendar_id: str = "primary"
    calendar_timezone: str = "America/Sao_Paulo"
    # Horário comercial pra slots oferecidos ao lead (decisão Mario
    # 2026-06-08: 14h-19h, slots de 30min, sem buffer).
    calendar_business_hours_start: int = Field(default=14, ge=0, le=23)
    calendar_business_hours_end: int = Field(default=19, ge=1, le=24)
    calendar_slot_min: int = Field(default=30, ge=15, le=180)
    calendar_buffer_min: int = Field(default=0, ge=0, le=60)
    calendar_lookahead_days: int = Field(default=5, ge=1, le=30)
    # Teto de slots oferecidos. Estratégia "escassez" (2 do primeiro dia
    # + 1 do seguinte + 1 do próximo) produz até 4.
    calendar_num_slots: int = Field(default=4, ge=1, le=10)

    # SMTP (Google Workspace) — disparo de email de aniversário aos
    # clientes. smtp_password é uma SENHA DE APP do Google (não a senha
    # da conta; gerar em myaccount.google.com/apppasswords, exige 2FA).
    # Vazio = emails desligados (só o alerta WhatsApp pro Mario).
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from_name: str = "Mario Noviello | Noviello Advocacia"

    # Dead-man's switch (healthchecks.io ou similar). Se preenchido, o
    # scheduler faz GET nesse URL ao fim de cada ciclo BEM-SUCEDIDO.
    # Se o serviço parar de pingar (timer travado, API key expirada,
    # crash em loop), o healthchecks alerta Mario por email. Vazio = off.
    healthcheck_ping_url: str = ""

    # Juridiq (gestão de processos) — intake automático. Quando o lead
    # agenda reunião via bot, cria a Pessoa no Juridiq com a
    # qualificação (nome, telefone, email, resumo, Meet). Chave criada
    # no painel do Juridiq. Vazio = feature off.
    juridiq_api_key: str = ""
    juridiq_base_url: str = "https://api.juridiq.com.br"

    # CUID do usuário "BOT IA" no Jurichat. Quando setado:
    #   1. start_human_support atribui conversas a ele (selectedUserId)
    #      em vez de sortear humano (isRandom) — conserta atribuição
    #      indevida pro "THS - Midia".
    #   2. Poll cycle pausa o bot (aguardando_humano) quando o ``user``
    #      da conversa é OUTRO — humano assumiu, bot não atropela.
    # Vazio = comportamento legado (isRandom, sem detecção).
    jurichat_bot_user_id: str = ""
