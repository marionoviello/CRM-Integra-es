"""SQLite connection and schema migrations.

Single migration block — applied idempotently on every startup. No
migration versioning needed for an MVP with a fixed schema.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    jurichat_lead_id         TEXT NOT NULL UNIQUE,
    jurichat_conversation_id TEXT NOT NULL,
    contato_telefone         TEXT NOT NULL,
    contato_nome             TEXT,
    estado                   TEXT NOT NULL,
    turnos                   INTEGER NOT NULL DEFAULT 0,
    ultima_msg_lead_em       TEXT,
    proxima_acao_em          TEXT,
    erro_atual               TEXT,
    ultimo_transcript_hash   TEXT,
    criado_em                TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_leads_proxima_acao
    ON leads(proxima_acao_em)
    WHERE proxima_acao_em IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_leads_estado ON leads(estado);

CREATE TABLE IF NOT EXISTS transicoes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_id         INTEGER NOT NULL REFERENCES leads(id),
    estado_anterior TEXT,
    estado_novo     TEXT NOT NULL,
    motivo          TEXT,
    payload_json    TEXT,
    criado_em       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_transicoes_lead ON transicoes(lead_id);

CREATE TABLE IF NOT EXISTS webhooks_recebidos (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    fonte           TEXT NOT NULL,
    evento_id       TEXT NOT NULL,
    hash_payload    TEXT NOT NULL,
    recebido_em     TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(fonte, evento_id)
);

-- Emails de aniversário enviados (idempotência: re-rodar o job no
-- mesmo dia não duplica o parabéns).
CREATE TABLE IF NOT EXISTS emails_aniversario (
    person_id   TEXT NOT NULL,
    enviado_em  TEXT NOT NULL,
    email       TEXT,
    UNIQUE(person_id, enviado_em)
);

-- Processos com monitoringStatus=ERRO já vistos pelo job de saúde da
-- carteira. Serve pra destacar 🆕 só os que entraram em erro desde a
-- última execução (em vez de repetir a lista inteira toda semana).
CREATE TABLE IF NOT EXISTS carteira_erro_visto (
    process_number TEXT PRIMARY KEY,
    primeiro_visto TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Falhas SILENCIOSAS (carteira_datajud): processos que o Juridiq mostra
-- como OK mas o cruzamento com o DataJud revela atrasados. Mesma ideia do
-- carteira_erro_visto: destacar 🆕 só os que entraram desde a última vez.
CREATE TABLE IF NOT EXISTS carteira_datajud_visto (
    process_number TEXT PRIMARY KEY,
    primeiro_visto TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Índice telefone→ficha do Juridiq (roadmap 0.1). Repovoado de
-- madrugada a partir do GET /person/ (que já traz phone/email/document).
-- Uma pessoa gera N linhas (variantes do número: com/sem 9º dígito).
-- Destrava reconhecer cliente existente e detectar conflito de interesse.
CREATE TABLE IF NOT EXISTS person_index (
    telefone_chave TEXT NOT NULL,
    person_id      TEXT NOT NULL,
    nome           TEXT,
    email          TEXT,
    document       TEXT,
    atualizado_em  TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (telefone_chave, person_id)
);

-- Emails que voltaram (bounce) — endereço inválido/morto. Os senders
-- (aniversário etc) consultam antes de enviar pra não insistir no que
-- nunca chega. Populada pelo detector_bounce ao cruzar devoluções da
-- caixa com o que o sistema registrou como enviado.
CREATE TABLE IF NOT EXISTS emails_mortos (
    email        TEXT PRIMARY KEY,
    motivo       TEXT,
    detectado_em TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Lista de SUPRESSÃO (opt-out / LGPD, roadmap 1.10). Quem pediu pra
-- parar de receber. Chave = telefone (variante canônica, com/sem 9º
-- dígito) OU email lowercase. TODOS os senders de relacionamento
-- (follow-up, reativação, aniversário) consultam antes de enviar.
CREATE TABLE IF NOT EXISTS opt_out (
    chave      TEXT PRIMARY KEY,   -- telefone-canônico ou email lowercase
    tipo       TEXT NOT NULL,      -- 'telefone' | 'email'
    motivo     TEXT,
    criado_em  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Partes CONTRÁRIAS dos processos (conflito de interesse, roadmap 1.7).
-- Repovoada de madrugada do GET /lawSuit/ (persons com personOrigin !=
-- Cliente). Quando um lead novo bate com um nome aqui, o bot LEVANTA
-- SUSPEITA ao Mario (nunca ao lead) — decisão é humana. Um nome pode
-- aparecer em vários processos.
CREATE TABLE IF NOT EXISTS parte_contraria (
    nome_norm  TEXT NOT NULL,
    processo   TEXT NOT NULL,
    papel      TEXT,
    PRIMARY KEY (nome_norm, processo)
);

CREATE INDEX IF NOT EXISTS idx_parte_nome ON parte_contraria(nome_norm);

-- Eventos financeiros já alertados (triagem_financeira, roadmap 2.10):
-- penhora/bloqueio/leilão (constrição) e RPV/precatório/alvará (dinheiro
-- a levantar) detectados nas movimentações do DataJud. Idempotência por
-- hash do evento (processo+data+nome) — um evento é um FATO que não
-- "desfaz", então só inserimos (nunca removemos): cada um alerta 1 vez.
CREATE TABLE IF NOT EXISTS triagem_financeira_visto (
    evento_hash    TEXT PRIMARY KEY,
    processo       TEXT NOT NULL,
    tipo           TEXT,            -- 'constricao' | 'levantar'
    primeiro_visto TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Índice telefone→processo DO CLIENTE (atendimento "como está meu
-- processo?", roadmap 2.4). Liga o telefone de quem manda WhatsApp ao(s)
-- processo(s) em que ele é PARTE CLIENTE — a autenticação: só passa info
-- pra quem está no cadastro. Repovoado de madrugada (junto do person_index)
-- cruzando GET /lawSuit/ (persons personOrigin=Cliente, por CPF/nome) com
-- o person_index (telefone↔CPF). is_secret marca segredo de justiça →
-- nunca responder automático, escalar pra Mario+Hilde. Uma pessoa pode
-- ter vários processos.
CREATE TABLE IF NOT EXISTS cliente_processo (
    telefone_chave     TEXT NOT NULL,
    person_id          TEXT,            -- ficha do Juridiq (autenticação forte)
    process_number     TEXT NOT NULL,
    is_secret          INTEGER NOT NULL DEFAULT 0,
    last_movement_date TEXT,
    cliente_nome       TEXT,
    match_tipo         TEXT,            -- 'cpf' (só CPF gera vínculo automático)
    atualizado_em      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (telefone_chave, process_number)
);

CREATE INDEX IF NOT EXISTS idx_cliente_processo_tel
    ON cliente_processo(telefone_chave);

-- Publicação urgente → TAREFA no Juridiq (roadmap 1.1). Idempotência por
-- publication_id: uma publicação só gera UMA tarefa, e a publicação só é
-- marcada como tratada depois da tarefa criada (não perde no meio).
CREATE TABLE IF NOT EXISTS tarefa_publicacao (
    publication_id TEXT PRIMARY KEY,
    process_number TEXT,
    task_id        TEXT,
    criada_em      TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Boletim mensal de andamento ao cliente (roadmap 3.1, variante mensal).
-- Idempotência por competência (YYYY-MM): o job roda no último dia útil
-- (e na janela até o fim do mês, p/ retry se o envio falhar), mas o lote
-- só é montado UMA vez por mês. Marcado só após o envio ao Mario dar certo.
CREATE TABLE IF NOT EXISTS boletim_competencia (
    competencia TEXT PRIMARY KEY,        -- 'YYYY-MM'
    total       INTEGER,
    enviado_em  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Contrato de honorários com assinatura eletrônica (ZapSign, roadmap 3.x).
-- Fluxo 1-TOQUE: o bot monta a minuta (estado pendente_aprovacao); o Mario
-- aprova UM contrato (estado aprovado); SÓ então o create-doc é chamado
-- (estado enviado); o webhook confirma (assinado). O create-doc NUNCA roda
-- fora do estado 'aprovado' — garantia OAB testada. valor_honorarios é
-- SEMPRE digitado por humano (a IA não precifica). zapsign_doc_token também
-- é a chave de idempotência do envio (não re-chama create-doc se já tem).
CREATE TABLE IF NOT EXISTS contrato (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id            TEXT,           -- ficha Juridiq do cliente (se já existe)
    lead_id              INTEGER,        -- vínculo ao lead do funil (se veio de lá)
    cliente_nome         TEXT NOT NULL,
    cliente_email        TEXT,
    cliente_telefone     TEXT,
    objeto               TEXT,           -- objeto do contrato (área/caso)
    valor_honorarios     TEXT NOT NULL,  -- texto livre, digitado pelo Mario
    estado               TEXT NOT NULL,  -- _pendente_aprovacao|_aprovado|_enviando|_enviado|_assinado|_recusado|_expirado
    template_id          TEXT,
    aprovacao_token      TEXT UNIQUE,    -- token único do link de aprovação 1-toque
    aprovado_em          TEXT,
    aprovado_por         TEXT,           -- auditoria: quem aprovou
    zapsign_doc_token    TEXT,           -- token do doc na ZapSign (após enviar) + idempotência
    zapsign_signer_token TEXT,
    sign_url             TEXT,
    signed_file_url      TEXT,           -- URL do PDF assinado (efêmero) → arquivamos
    criado_em            TEXT NOT NULL DEFAULT (datetime('now')),
    atualizado_em        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contrato_estado ON contrato(estado);
CREATE INDEX IF NOT EXISTS idx_contrato_doc_token
    ON contrato(zapsign_doc_token) WHERE zapsign_doc_token IS NOT NULL;

-- Trilha de auditoria de CADA transição do contrato (quem, quando, por quê).
-- É isto que torna o 1-toque defensável perante a OAB: prova que o envio só
-- aconteceu depois de uma aprovação humana registrada.
CREATE TABLE IF NOT EXISTS contrato_transicao (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contrato_id     INTEGER NOT NULL REFERENCES contrato(id),
    estado_anterior TEXT,
    estado_novo     TEXT NOT NULL,
    motivo          TEXT,
    ator            TEXT,                -- 'mario' | 'webhook' | 'sistema'
    criado_em       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_contrato_transicao_contrato
    ON contrato_transicao(contrato_id);
"""


def connect(database_path: str) -> sqlite3.Connection:
    """Open SQLite connection with sensible defaults for this app."""
    if database_path != ":memory:":
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        database_path,
        timeout=30,
        isolation_level=None,  # autocommit; we use explicit transactions
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply schema. Idempotent — uses IF NOT EXISTS everywhere.

    Extra step: handle column additions on existing tables. SQLite has no
    `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, so we use try/except to
    make these idempotent.
    """
    conn.executescript(SCHEMA)
    _ensure_column(conn, "leads", "ultimo_transcript_hash", "TEXT")
    # Reunião agendada via Calendar (feature lembretes 2026-06-08).
    # reuniao_em = quando vai rolar (ISO datetime); event_id pra cancelar
    # via Google; meet_link pra reaproveitar nos lembretes.
    _ensure_column(conn, "leads", "reuniao_em", "TEXT")
    _ensure_column(conn, "leads", "reuniao_event_id", "TEXT")
    _ensure_column(conn, "leads", "reuniao_meet_link", "TEXT")
    # Timestamp do envio de cada lembrete (NULL = ainda não enviado).
    # Usamos timestamp em vez de bool pra facilitar debug/auditoria.
    _ensure_column(conn, "leads", "lembrete_24h_enviado_em", "TEXT")
    _ensure_column(conn, "leads", "lembrete_2h_enviado_em", "TEXT")
    _ensure_column(conn, "leads", "lembrete_30min_enviado_em", "TEXT")
    _ensure_column(conn, "leads", "lembrete_5min_enviado_em", "TEXT")
    # No-show: token do link de 1 toque que o Mario recebe 5 min após o
    # início (NULL = ping ainda não enviado; setado = já avisado + link vivo).
    _ensure_column(conn, "leads", "noshow_token", "TEXT")
    # Escalonamento de urgência jurídica (roadmap 1.12). Timestamp do
    # alerta 🚨 ao Mario — NULL = ainda não escalado. Evita repetir o
    # alerta a cada mensagem do lead urgente.
    _ensure_column(conn, "leads", "urgencia_alertada_em", "TEXT")
    # F1 (auditoria 24/jun): escalonamento de erros. erro_consecutivo conta
    # falhas seguidas (register_error incrementa, update_transcript_hash zera =
    # progresso); ao cruzar o limiar, o poll cycle alerta o Mario UMA vez e
    # carimba erro_alertado_em (antes erro_atual era write-only → lead preso em
    # falha de API ficava mudo dias e o Mario nunca sabia).
    _ensure_column(conn, "leads", "erro_consecutivo", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "leads", "erro_alertado_em", "TEXT")
    # Reconhecer cliente existente (roadmap 1.6). Timestamp do check
    # contra o person_index — NULL = ainda não checado. Roda 1x por lead.
    _ensure_column(conn, "leads", "cliente_checado_em", "TEXT")
    # Horários que o bot ACABOU de oferecer (bugfix Camila 16/jun). JSON
    # ``[{"iso","label"}]`` — preenchido em oferecer_horarios, lido pela
    # escolha determinística (Signal 1.8), limpo ao confirmar/cancelar.
    # Tira o Claude do caminho crítico "lead escolhe horário → confirma".
    _ensure_column(conn, "leads", "horarios_oferecidos", "TEXT")
    # Pipeline de fechamento escopos→Asaas→ZapSign com gate humano sobre o
    # PDF REAL (roadmap 3.x). tipo_caso seleciona o escopo curado; cpf é PII
    # obrigatória pro Asaas; asaas_* guardam a cobrança (dedupe + cancelamento);
    # invoice_url vira o {{LINK_PAGAMENTO}} do contrato; cobranca_paga_em é
    # carimbado pelo webhook Asaas; reprovacao_token é o link 1-toque de REPROVAR
    # (distinto do aprovacao_token de aprovar).
    _ensure_column(conn, "contrato", "tipo_caso", "TEXT")
    _ensure_column(conn, "contrato", "cpf", "TEXT")
    _ensure_column(conn, "contrato", "asaas_customer_id", "TEXT")
    _ensure_column(conn, "contrato", "asaas_payment_id", "TEXT")
    _ensure_column(conn, "contrato", "invoice_url", "TEXT")
    _ensure_column(conn, "contrato", "cobranca_paga_em", "TEXT")
    _ensure_column(conn, "contrato", "reprovacao_token", "TEXT")
    # Idempotência por chave de negócio (cpf só-dígitos + tipo_caso) enquanto
    # o contrato está ABERTO (montagem/criando_doc/pendente_revisao): impede
    # que um duplo-comando "gerar contrato" crie 2 contratos/2 cobranças pro
    # mesmo cliente/caso. cpf é gravado já normalizado (re.sub r'\D'); o índice
    # parcial é a rede de defesa por baixo do lookup explícito do orquestrador.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_contrato_aberto "
        "ON contrato(cpf, tipo_caso) WHERE estado IN ("
        "'contrato_montagem', 'contrato_criando_doc', 'contrato_pendente_revisao'"
        ")"
    )


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, type_: str,
) -> None:
    """Idempotent ADD COLUMN — no-ops if column already exists."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
