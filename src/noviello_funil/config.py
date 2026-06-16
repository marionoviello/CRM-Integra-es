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
    # IMAP (mesma conta/senha de app do SMTP) — o detector_bounce lê a
    # caixa pra achar devoluções e avisar quando um email não chegou ao
    # cliente. Exige IMAP habilitado na conta Workspace.
    imap_host: str = "imap.gmail.com"
    imap_port: int = 993

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

    # DataJud (CNJ) — cruzamento da carteira pra pegar falhas SILENCIOSAS
    # do monitoramento do Juridiq (carteira_datajud). A chave abaixo é a
    # chave PÚBLICA oficial do CNJ (publicada em datajud-wiki.cnj.jus.br),
    # igual pra todo mundo — não é segredo, daí o default no código.
    datajud_api_key: str = (
        "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="
    )
    # Alerta quando o DataJud está mais que N dias à frente do Juridiq.
    # 30 dias filtra ruído de cadência de sync (casos reais eram anos).
    carteira_datajud_limiar_dias: int = Field(default=30, ge=1)
    # Triagem financeira (penhora/RPV/precatório/leilão nas movimentações):
    # janela de quanto tempo pra trás varrer eventos. 120 dias na 1ª rodada
    # pega o backlog recente; depois a idempotência só traz os novos.
    triagem_financeira_janela_dias: int = Field(default=120, ge=1)
    # Boletim mensal de andamento ao cliente (3.1). True força a rodada
    # mesmo fora do último dia útil — só pra smoke/teste manual.
    boletim_forcar: bool = False

    # 1.1: publicação urgente vira TAREFA no Juridiq. publicacoes_criar_tarefa
    # liga o POST /task/ (default OFF — só alerta, como hoje, até validar).
    # task_column_id é o UUID da coluna do kanban onde a tarefa nasce (o POST
    # exige columnId, não o nome — descoberto 15/jun). Preencher no .env com o
    # id da coluna "Pendente" (achado pelo diagnóstico). Vazio = não cria.
    publicacoes_criar_tarefa: bool = False
    task_column_id: str = ""
    task_priority: str = "Alta"

    # ZapSign — fechamento de contrato com assinatura eletrônica (3.x).
    # Fluxo 1-TOQUE: o bot monta a minuta e o Mario aprova UM contrato por
    # vez. O create-doc SÓ roda depois da aprovação humana — nunca 100%
    # automático (Prov. 205/2021, mandato personalíssimo). contratos_zapsign
    # liga a feature (default OFF). Token e secret no .env (gitignored),
    # nunca no código. Escopo inicial: SÓ contrato de honorários (procuração
    # fica fora até confirmar aceitação no foro — decisão 15/jun).
    contratos_zapsign: bool = False
    zapsign_api_token: str = ""
    zapsign_base_url: str = "https://api.zapsign.com.br/api/v1"
    # A ZapSign NÃO assina o webhook com HMAC — a segurança é um header
    # secreto que cadastramos junto do webhook e ela devolve em cada POST.
    # Validado constant-time no /webhooks/zapsign. Gerar valor longo aleatório.
    zapsign_webhook_secret: str = ""
    # Template DOCX de CONTRATO DE HONORÁRIOS no painel ZapSign (placeholders
    # {{...}}). Vazio = não gera. O mapa placeholder→campo é injetado em
    # runtime (não hardcodar os nomes do template do Mario).
    zapsign_template_honorarios_id: str = ""
    # Base URL pública do funil (ex.: https://funil.noviello.adv.br) — monta o
    # link de aprovação 1-toque que vai pro WhatsApp do Mario e o endpoint do
    # webhook cadastrado na ZapSign. Vazio = links de aprovação quebrados.
    funil_base_url: str = ""

    # Asaas — cobrança de honorários no fechamento de contrato (pipeline 3.x).
    # SÓ cria/cancela cobrança PENDENTE (faturamento do escritório) — NUNCA
    # estorno/transferência/saque. contratos_asaas liga a feature (default OFF).
    # Header de auth é ``access_token`` (não Bearer). Sandbox-first: a base de
    # teste não cobra de verdade. Chave/token só no .env (gitignored).
    contratos_asaas: bool = False
    asaas_api_key: str = ""
    asaas_base_url: str = "https://api-sandbox.asaas.com"
    # Token compartilhado que volta no header ``asaas-access-token`` de cada
    # webhook (a Asaas não assina com HMAC) — validado constant-time.
    asaas_webhook_token: str = ""
    asaas_user_agent: str = "noviello-bot/1.0"
    # Vencimento default da cobrança quando o Mario não especifica (dias).
    asaas_payment_due_days: int = Field(default=7, ge=1)

    # Signatários FIXOS do contrato (caminho A): o ESCRITÓRIO (Mario)
    # contra-assina depois do cliente (order_group 2), e 2 TESTEMUNHAS
    # (order_group 3) tornam o contrato executável (CPC 784 IV). CPF/email
    # são PII — só no .env, nunca versionados. Vazio = signatário omitido.
    contrato_escritorio_nome: str = ""
    contrato_escritorio_email: str = ""
    contrato_escritorio_cpf: str = ""
    contrato_testemunha_1_nome: str = ""
    contrato_testemunha_1_email: str = ""
    contrato_testemunha_1_cpf: str = ""
    contrato_testemunha_2_nome: str = ""
    contrato_testemunha_2_email: str = ""
    contrato_testemunha_2_cpf: str = ""

    # CUID do usuário "BOT IA" no Jurichat. Quando setado:
    #   1. start_human_support atribui conversas a ele (selectedUserId)
    #      em vez de sortear humano (isRandom) — conserta atribuição
    #      indevida pro "THS - Midia".
    #   2. Poll cycle pausa o bot (aguardando_humano) quando o ``user``
    #      da conversa é OUTRO — humano assumiu, bot não atropela.
    # Vazio = comportamento legado (isRandom, sem detecção).
    jurichat_bot_user_id: str = ""
