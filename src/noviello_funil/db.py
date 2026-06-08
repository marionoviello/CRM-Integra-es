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


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, type_: str,
) -> None:
    """Idempotent ADD COLUMN — no-ops if column already exists."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {type_}")
    except sqlite3.OperationalError as exc:
        if "duplicate column name" not in str(exc).lower():
            raise
