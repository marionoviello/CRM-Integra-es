"""State repository: the ONLY module that reads/writes SQLite.

Estado is a string enum kept as plain TEXT in the DB (no CHECK constraint
to keep migrations forgiving). All transitions go through explicit
functions defined here — never UPDATE estado from outside this module.
"""

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final


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


# --- Transitions and updates ---------------------------------------------

# Sentinel for transicao(): pass CLEAR_PROXIMA_ACAO to explicitly clear
# the schedule in the same transaction. Distinct from `None`, which means
# "don't touch proxima_acao_em".
CLEAR_PROXIMA_ACAO: Final = object()


def transicao(
    conn: sqlite3.Connection,
    lead_id: int,
    estado_novo: str,
    *,
    motivo: str,
    payload: dict[str, Any] | None = None,
    proxima_acao_horas: int | object | None = None,
) -> None:
    """Atomic state transition: update lead.estado AND insert transicoes row.

    Always go through this function — never naked UPDATE estado.

    BEGIN IMMEDIATE acquires the write lock up front so the SELECT-then-UPDATE
    sequence cannot race against a concurrent writer on a different connection.
    Under the current single-shared-connection setup this is belt-and-suspenders
    (Python's sqlite3 module serializes ops on a connection), but it stays
    correct if we ever move to per-task connections.

    proxima_acao_horas behavior (atomic with the transition):
        - None (default):       proxima_acao_em is NOT touched
        - int N:                proxima_acao_em = now + N hours
        - CLEAR_PROXIMA_ACAO:   proxima_acao_em set to NULL

    Why this matters: the scheduler sends a non-idempotent WhatsApp message
    after transitioning. If state changed but proxima_acao_em didn't, the
    lead would be re-picked-up on the next tick and the message would fire
    twice. Wrapping both updates in one transaction prevents the race.
    """
    payload_json = json.dumps(payload) if payload is not None else None

    schedule_clause = ""
    schedule_params: tuple = ()
    if proxima_acao_horas is CLEAR_PROXIMA_ACAO:
        schedule_clause = ", proxima_acao_em = NULL"
    elif isinstance(proxima_acao_horas, int):
        proxima = (
            datetime.utcnow() + timedelta(hours=proxima_acao_horas)
        ).strftime("%Y-%m-%d %H:%M:%S")
        schedule_clause = ", proxima_acao_em = ?"
        schedule_params = (proxima,)

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT estado FROM leads WHERE id = ?", (lead_id,)
        ).fetchone()
        if current is None:
            conn.execute("ROLLBACK")
            raise ValueError(f"Lead {lead_id} not found")
        estado_anterior = current["estado"]

        conn.execute(
            "UPDATE leads SET estado = ?, atualizado_em = datetime('now')"
            + schedule_clause
            + " WHERE id = ?",
            (estado_novo, *schedule_params, lead_id),
        )
        conn.execute(
            """
            INSERT INTO transicoes (lead_id, estado_anterior, estado_novo, motivo, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (lead_id, estado_anterior, estado_novo, motivo, payload_json),
        )
        conn.execute("COMMIT")
    except Exception:
        # ROLLBACK may itself fail if already rolled back via the ValueError
        # path above — suppress that specific error and re-raise the original.
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("ROLLBACK")
        raise


def bump_turnos(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute(
        "UPDATE leads SET turnos = turnos + 1, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (lead_id,),
    )


def record_lead_message_received(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    proxima_acao_horas: int,
    reset_turnos: bool = False,
) -> None:
    """Mark that a new message from the lead arrived.

    Updates ultima_msg_lead_em=now, schedules proxima_acao_em=now+H hours.
    If reset_turnos=True, also resets turnos to 0 (used when reopening
    from encerrado_sem_resposta).
    """
    proxima = (datetime.utcnow() + timedelta(hours=proxima_acao_horas)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    if reset_turnos:
        conn.execute(
            """
            UPDATE leads SET
                ultima_msg_lead_em = datetime('now'),
                proxima_acao_em = ?,
                turnos = 0,
                atualizado_em = datetime('now')
            WHERE id = ?
            """,
            (proxima, lead_id),
        )
    else:
        conn.execute(
            """
            UPDATE leads SET
                ultima_msg_lead_em = datetime('now'),
                proxima_acao_em = ?,
                atualizado_em = datetime('now')
            WHERE id = ?
            """,
            (proxima, lead_id),
        )


def schedule_next_action(
    conn: sqlite3.Connection, lead_id: int, horas: int,
) -> None:
    """Set proxima_acao_em = now + horas. Used by scheduler after sending follow-up."""
    proxima = (datetime.utcnow() + timedelta(hours=horas)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "UPDATE leads SET proxima_acao_em = ?, atualizado_em = datetime('now') WHERE id = ?",
        (proxima, lead_id),
    )


def schedule_next_action_seconds(
    conn: sqlite3.Connection, lead_id: int, seconds: int,
) -> None:
    """Set proxima_acao_em = now + seconds. For sub-hour polling cadence."""
    proxima = (datetime.utcnow() + timedelta(seconds=seconds)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    conn.execute(
        "UPDATE leads SET proxima_acao_em = ?, atualizado_em = datetime('now') WHERE id = ?",
        (proxima, lead_id),
    )


def mark_lead_activity_now(conn: sqlite3.Connection, lead_id: int) -> None:
    """Stamp ultima_msg_lead_em = now without touching proxima_acao_em.

    Used by the poll cycle when it detects a transcript change: this
    ensures the follow-up cycle's "idle > 24h" carve-out treats the lead
    as freshly active and does NOT nudge it. The poll cycle itself
    schedules the next poll separately via schedule_next_action_seconds.
    """
    conn.execute(
        "UPDATE leads SET ultima_msg_lead_em = datetime('now'), "
        "atualizado_em = datetime('now') WHERE id = ?",
        (lead_id,),
    )


def clear_next_action(conn: sqlite3.Connection, lead_id: int) -> None:
    conn.execute(
        "UPDATE leads SET proxima_acao_em = NULL, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (lead_id,),
    )


def register_error(
    conn: sqlite3.Connection, lead_id: int, erro: str | None,
) -> None:
    conn.execute(
        "UPDATE leads SET erro_atual = ?, atualizado_em = datetime('now') WHERE id = ?",
        (erro, lead_id),
    )


def get_transcript_hash(
    conn: sqlite3.Connection, lead_id: int,
) -> str | None:
    """Return the hash of the last transcript the scheduler processed,
    or None if no transcript has been recorded yet."""
    row = conn.execute(
        "SELECT ultimo_transcript_hash FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    return row["ultimo_transcript_hash"] if row else None


def update_transcript_hash(
    conn: sqlite3.Connection, lead_id: int, transcript_hash: str,
) -> None:
    """Record the hash of the transcript we just processed. Used by the
    polling scheduler to detect new messages on the next tick."""
    conn.execute(
        "UPDATE leads SET ultimo_transcript_hash = ?, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (transcript_hash, lead_id),
    )


def list_leads_vencidos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """For the follow-up cycle: leads whose proxima_acao_em has passed AND
    are in a non-terminal state.

    Carve-out: em_conversa leads with activity in the last 24h are EXCLUDED
    here — they are owned by the polling cycle (which reschedules
    proxima_acao_em every minute). Without this exclusion the follow-up
    cycle would fire immediately for every active polled conversation.

    em_conversa leads with NO recorded activity (ultima_msg_lead_em IS NULL)
    or last activity > 24h ago ARE included — those are genuinely idle and
    should receive a follow-up nudge.
    """
    return conn.execute(
        """
        SELECT * FROM leads
        WHERE proxima_acao_em IS NOT NULL
          AND proxima_acao_em < datetime('now')
          AND estado IN (?, ?, ?)
          AND (
              estado != ?
              OR ultima_msg_lead_em IS NULL
              OR ultima_msg_lead_em < datetime('now', '-24 hours')
          )
        ORDER BY proxima_acao_em ASC
        """,
        (
            Estado.EM_CONVERSA,
            Estado.FOLLOW_UP_1_ENVIADO,
            Estado.FOLLOW_UP_2_ENVIADO,
            Estado.EM_CONVERSA,
        ),
    ).fetchall()


# --- Reuniões agendadas + lembretes -------------------------------------

def set_reuniao(
    conn: sqlite3.Connection,
    lead_id: int,
    *,
    reuniao_em_iso: str,
    event_id: str,
    meet_link: str,
) -> None:
    """Salva reunião marcada via Calendar. Reseta os 3 flags de lembrete.

    Se ``reuniao_em`` já está dentro de janela de lembrete (< threshold),
    o reminder_cycle vai disparar imediatamente no próximo tick — então
    aqui marcamos os lembretes "tarde" como já enviados pra evitar
    confusão. Ex: reunião marcada pra daqui a 90 min → lembrete 24h não
    faz sentido, marca como enviado.
    """
    now = datetime.now(UTC)
    try:
        reuniao_dt = datetime.fromisoformat(reuniao_em_iso).astimezone(UTC)
    except ValueError:
        reuniao_dt = now  # parse falhou — vamos depender do reminder_cycle pra logar
    delta = reuniao_dt - now

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    lembrete_24h_sent = now_str if delta < timedelta(hours=24) else None
    lembrete_2h_sent = now_str if delta < timedelta(hours=2) else None
    lembrete_30min_sent = now_str if delta < timedelta(minutes=30) else None

    conn.execute(
        """
        UPDATE leads SET
            reuniao_em = ?,
            reuniao_event_id = ?,
            reuniao_meet_link = ?,
            lembrete_24h_enviado_em = ?,
            lembrete_2h_enviado_em = ?,
            lembrete_30min_enviado_em = ?,
            atualizado_em = datetime('now')
        WHERE id = ?
        """,
        (
            reuniao_em_iso, event_id, meet_link,
            lembrete_24h_sent, lembrete_2h_sent, lembrete_30min_sent,
            lead_id,
        ),
    )


def clear_reuniao(conn: sqlite3.Connection, lead_id: int) -> None:
    """Limpa reunião marcada (cancelamento ou já passou)."""
    conn.execute(
        """
        UPDATE leads SET
            reuniao_em = NULL,
            reuniao_event_id = NULL,
            reuniao_meet_link = NULL,
            lembrete_24h_enviado_em = NULL,
            lembrete_2h_enviado_em = NULL,
            lembrete_30min_enviado_em = NULL,
            atualizado_em = datetime('now')
        WHERE id = ?
        """,
        (lead_id,),
    )


def mark_lembrete_enviado(
    conn: sqlite3.Connection, lead_id: int, lembrete: str,
) -> None:
    """``lembrete`` ∈ {'24h', '2h', '30min'}. Idempotente."""
    coluna_map = {
        "24h": "lembrete_24h_enviado_em",
        "2h": "lembrete_2h_enviado_em",
        "30min": "lembrete_30min_enviado_em",
    }
    coluna = coluna_map[lembrete]
    conn.execute(
        f"UPDATE leads SET {coluna} = datetime('now'), "
        "atualizado_em = datetime('now') WHERE id = ?",
        (lead_id,),
    )


def list_leads_com_reuniao_futura(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Leads com reuniao_em > now (apenas reuniões futuras).

    Independente do estado (em_conversa, aguardando_humano, etc.) — o
    reminder cycle precisa cobrir todos. Reuniões já passadas são
    limpadas pelo cycle (clear_reuniao).
    """
    return conn.execute(
        """
        SELECT * FROM leads
        WHERE reuniao_em IS NOT NULL
        ORDER BY reuniao_em ASC
        """,
    ).fetchall()


def list_leads_para_polling(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """For the polling cycle: em_conversa leads whose poll tick is due.

    Strictly em_conversa — follow-up states are handled by list_leads_vencidos.
    """
    return conn.execute(
        """
        SELECT * FROM leads
        WHERE estado = ?
          AND proxima_acao_em IS NOT NULL
          AND proxima_acao_em < datetime('now')
        ORDER BY proxima_acao_em ASC
        """,
        (Estado.EM_CONVERSA,),
    ).fetchall()
