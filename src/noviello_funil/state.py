"""State repository: the ONLY module that reads/writes SQLite.

Estado is a string enum kept as plain TEXT in the DB (no CHECK constraint
to keep migrations forgiving). All transitions go through explicit
functions defined here — never UPDATE estado from outside this module.
"""

import sqlite3
from dataclasses import dataclass
from typing import Final


class Estado:
    """All valid lead states. Used as string constants in DB."""
    EM_CONVERSA: Final = "em_conversa"
    FOLLOW_UP_1_ENVIADO: Final = "follow_up_1_enviado"
    FOLLOW_UP_2_ENVIADO: Final = "follow_up_2_enviado"
    ENCERRADO_SEM_RESPOSTA: Final = "encerrado_sem_resposta"
    AGUARDANDO_HUMANO: Final = "aguardando_humano"


# Estados em que Claude ainda pode atuar (responder/avaliar nova msg)
ESTADOS_ATIVOS_CLAUDE: Final = frozenset({
    Estado.EM_CONVERSA,
    Estado.FOLLOW_UP_1_ENVIADO,
    Estado.FOLLOW_UP_2_ENVIADO,
    Estado.ENCERRADO_SEM_RESPOSTA,
})


@dataclass
class Lead:
    """In-memory lead row. Use sqlite3.Row directly when possible —
    this dataclass exists for type-safe parameter passing."""
    id: int
    jurichat_lead_id: str
    jurichat_conversation_id: str
    contato_telefone: str
    contato_nome: str | None
    estado: str
    turnos: int
    ultima_msg_lead_em: str | None
    proxima_acao_em: str | None
    erro_atual: str | None


def create_lead_if_absent(
    conn: sqlite3.Connection,
    jurichat_lead_id: str,
    jurichat_conversation_id: str,
    contato_telefone: str,
    contato_nome: str | None,
) -> sqlite3.Row:
    """Insert a new lead in EM_CONVERSA state if jurichat_lead_id is new.

    Returns the existing or newly-created row.
    """
    existing = conn.execute(
        "SELECT * FROM leads WHERE jurichat_lead_id = ?",
        (jurichat_lead_id,),
    ).fetchone()
    if existing is not None:
        return existing

    conn.execute(
        """
        INSERT INTO leads (
            jurichat_lead_id, jurichat_conversation_id,
            contato_telefone, contato_nome, estado, turnos
        ) VALUES (?, ?, ?, ?, ?, 0)
        """,
        (
            jurichat_lead_id,
            jurichat_conversation_id,
            contato_telefone,
            contato_nome,
            Estado.EM_CONVERSA,
        ),
    )
    return conn.execute(
        "SELECT * FROM leads WHERE jurichat_lead_id = ?",
        (jurichat_lead_id,),
    ).fetchone()


def get_lead_by_conversation(
    conn: sqlite3.Connection, jurichat_conversation_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leads WHERE jurichat_conversation_id = ?",
        (jurichat_conversation_id,),
    ).fetchone()


def get_lead_by_id(
    conn: sqlite3.Connection, lead_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM leads WHERE id = ?",
        (lead_id,),
    ).fetchone()


def is_webhook_processed(
    conn: sqlite3.Connection, fonte: str, evento_id: str,
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM webhooks_recebidos WHERE fonte = ? AND evento_id = ?",
        (fonte, evento_id),
    ).fetchone()
    return row is not None


def mark_webhook_processed(
    conn: sqlite3.Connection, fonte: str, evento_id: str, hash_payload: str,
) -> None:
    """Insert into webhooks_recebidos. Silently no-ops on duplicate."""
    conn.execute(
        """
        INSERT OR IGNORE INTO webhooks_recebidos (fonte, evento_id, hash_payload)
        VALUES (?, ?, ?)
        """,
        (fonte, evento_id, hash_payload),
    )
