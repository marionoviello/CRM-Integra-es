"""State repository: the ONLY module that reads/writes SQLite.

Estado is a string enum kept as plain TEXT in the DB (no CHECK constraint
to keep migrations forgiving). All transitions go through explicit
functions defined here — never UPDATE estado from outside this module.
"""

import contextlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

# H1 (auditoria 24/jun): retry de "database is locked" no caminho de escrita
# crítico (transicao). Web e scheduler são processos SEPARADOS → conexões
# distintas; sob colisão o BEGIN IMMEDIATE pode receber SQLITE_BUSY mesmo com o
# busy_timeout de 30s (caso de potencial deadlock, em que o busy handler não
# espera). Backoff curto: 50ms, 100ms, 200ms.
_MAX_RETRY_LOCK: Final = 4
_RETRY_LOCK_BASE: Final = 0.05


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

    # H1 (auditoria 24/jun): re-tenta a transação inteira em "database is locked"
    # (colisão web×scheduler). BEGIN IMMEDIATE está DENTRO do try pra um lock no
    # próprio BEGIN ser capturado. A transação é atômica (BEGIN…COMMIT, ROLLBACK
    # no erro), então re-rodar é seguro. Esgotou as tentativas → propaga (vai pro
    # register_error/alerta do caller).
    for _tentativa in range(_MAX_RETRY_LOCK):
        try:
            conn.execute("BEGIN IMMEDIATE")
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
            return
        except sqlite3.OperationalError as exc:
            with contextlib.suppress(sqlite3.OperationalError):
                conn.execute("ROLLBACK")
            esgotou = _tentativa == _MAX_RETRY_LOCK - 1
            if "locked" not in str(exc).lower() or esgotou:
                raise
            time.sleep(_RETRY_LOCK_BASE * (2 ** _tentativa))
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


def reset_turnos(conn: sqlite3.Connection, lead_id: int) -> None:
    """Zera o contador de turnos. Usado na reativação de um lead — o teto de
    turnos mede a conversa ATIVA, não o histórico vitalício (P0 24/jun)."""
    conn.execute(
        "UPDATE leads SET turnos = 0, atualizado_em = datetime('now') "
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
    # F1 (auditoria 24/jun): além de gravar o último erro, incrementa o contador
    # de falhas CONSECUTIVAS. O poll cycle alerta o Mario quando cruza o limiar
    # (lead preso em falha de API recorrente não fica mais mudo/invisível). O
    # contador zera em update_transcript_hash (= o lead progrediu).
    conn.execute(
        "UPDATE leads SET erro_atual = ?, "
        "erro_consecutivo = erro_consecutivo + 1, "
        "atualizado_em = datetime('now') WHERE id = ?",
        (erro, lead_id),
    )


def list_leads_presos(
    conn: sqlite3.Connection, min_erros: int,
) -> list[sqlite3.Row]:
    """F1 (auditoria 24/jun): leads com >= ``min_erros`` falhas CONSECUTIVAS que
    ainda NÃO foram alertados (erro_alertado_em IS NULL). Usado pelo poll cycle
    pra avisar o Mario UMA vez sobre um lead preso em falha recorrente.

    Restrito aos estados em que o bot ATIVAMENTE re-tenta (em_conversa via poll,
    FU1/FU2 via follow-up). Exclui terminais (aguardando_humano/encerrado): lá o
    bot NÃO está mais tentando, então o alerta "preso, segue tentando" seria
    falso e contradiria o handoff que o Mario já recebeu — um lead pode chegar a
    terminal carregando erro_consecutivo>=3 acumulado no follow-up cycle, que não
    reseta o contador (revisão adversarial 24/jun)."""
    return conn.execute(
        "SELECT * FROM leads WHERE erro_consecutivo >= ? "
        "AND erro_alertado_em IS NULL AND estado IN (?, ?, ?)",
        (
            min_erros, Estado.EM_CONVERSA,
            Estado.FOLLOW_UP_1_ENVIADO, Estado.FOLLOW_UP_2_ENVIADO,
        ),
    ).fetchall()


def marcar_erro_alertado(conn: sqlite3.Connection, lead_id: int) -> None:
    """Carimba erro_alertado_em = now → o alerta de 'lead preso' sai UMA vez
    (não a cada tick enquanto a falha durar). Zerado em update_transcript_hash."""
    conn.execute(
        "UPDATE leads SET erro_alertado_em = datetime('now'), "
        "atualizado_em = datetime('now') WHERE id = ?",
        (lead_id,),
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
    # F1 (auditoria 24/jun): atualizar o hash = o lead PROGREDIU (mensagem
    # processada/enviada com sucesso) → zera o contador de falhas consecutivas
    # e o carimbo de alerta, pra um lead que se recuperou poder voltar a alertar
    # se travar de novo no futuro.
    conn.execute(
        "UPDATE leads SET ultimo_transcript_hash = ?, "
        "erro_consecutivo = 0, erro_alertado_em = NULL, "
        "atualizado_em = datetime('now') WHERE id = ?",
        (transcript_hash, lead_id),
    )


def mark_urgencia_alertada(conn: sqlite3.Connection, lead_id: int) -> None:
    """Stamp urgencia_alertada_em = now (roadmap 1.12). Garante que o
    alerta 🚨 de urgência sai UMA vez por lead, não a cada mensagem."""
    conn.execute(
        "UPDATE leads SET urgencia_alertada_em = datetime('now'), "
        "atualizado_em = datetime('now') WHERE id = ?",
        (lead_id,),
    )


def mark_cliente_checado(conn: sqlite3.Connection, lead_id: int) -> None:
    """Stamp cliente_checado_em = now (roadmap 1.6). O reconhecimento de
    cliente roda UMA vez por lead (não a cada mensagem)."""
    conn.execute(
        "UPDATE leads SET cliente_checado_em = datetime('now'), "
        "atualizado_em = datetime('now') WHERE id = ?",
        (lead_id,),
    )


# --- Horários oferecidos (escolha determinística, bugfix Camila 16/jun) ---

def set_horarios_oferecidos(
    conn: sqlite3.Connection, lead_id: int, slots: list[dict],
) -> None:
    """Persiste os horários que o bot acabou de oferecer (JSON [{iso,label}])."""
    conn.execute(
        "UPDATE leads SET horarios_oferecidos = ?, "
        "atualizado_em = datetime('now') WHERE id = ?",
        (json.dumps(slots), lead_id),
    )


def get_horarios_oferecidos(
    conn: sqlite3.Connection, lead_id: int,
) -> list[dict]:
    """Horários pendentes de escolha (vazio se nenhum/ inválido)."""
    row = conn.execute(
        "SELECT horarios_oferecidos FROM leads WHERE id = ?", (lead_id,),
    ).fetchone()
    if not row or not row["horarios_oferecidos"]:
        return []
    try:
        dados = json.loads(row["horarios_oferecidos"])
    except (ValueError, TypeError):
        return []
    if not isinstance(dados, list):
        return []
    # Filtra itens inválidos (strings soltas, None, dicts sem "iso") na borda
    # de leitura — o consumidor (matcher / Signal 1.8) só vê dicts com iso.
    return [
        d for d in dados
        if isinstance(d, dict) and isinstance(d.get("iso"), str) and d["iso"]
    ]


def clear_horarios_oferecidos(conn: sqlite3.Connection, lead_id: int) -> None:
    """Limpa os horários pendentes (após confirmar/cancelar)."""
    conn.execute(
        "UPDATE leads SET horarios_oferecidos = NULL, "
        "atualizado_em = datetime('now') WHERE id = ?",
        (lead_id,),
    )


def list_leads_vencidos(
    conn: sqlite3.Connection, *, fu1_apos_horas: int = 48,
) -> list[sqlite3.Row]:
    """For the follow-up cycle.

    Auditoria 2026-06-11 — dois HIGHs corrigidos nesta query:

    1. STARVATION do FU1: o critério antigo pra em_conversa era
       ``proxima_acao_em < now`` — mas o poll cycle REAGENDA
       proxima_acao_em a cada tick (+60s), então o follow-up só via o
       lead na janela de corrida intra-tick. Lead idle podia ficar
       semanas sem FU1. Agora em_conversa usa um relógio próprio:
       ``ultima_msg_lead_em`` (ou criado_em, se nunca houve msg) mais
       velho que ``fu1_apos_horas``.

    2. Lead com REUNIÃO MARCADA entrava no funil de follow-up
       ("percebi que talvez não seja o momento...") enquanto o
       reminder cycle mandava lembretes da reunião — mensagens
       contraditórias pra lead convertido. ``reuniao_em IS NULL`` em
       todos os ramos.

    FU1/FU2 continuam pelo relógio próprio (proxima_acao_em, setado
    na transição com +72h/+24h).
    """
    horas = int(fu1_apos_horas)
    return conn.execute(
        f"""
        SELECT * FROM leads
        WHERE reuniao_em IS NULL
          AND (
            (
              estado = ?
              AND COALESCE(ultima_msg_lead_em, criado_em)
                  < datetime('now', '-{horas} hours')
            )
            OR (
              estado IN (?, ?)
              AND proxima_acao_em IS NOT NULL
              AND proxima_acao_em < datetime('now')
            )
          )
        ORDER BY atualizado_em ASC
        """,
        (
            Estado.EM_CONVERSA,
            Estado.FOLLOW_UP_1_ENVIADO,
            Estado.FOLLOW_UP_2_ENVIADO,
        ),
    ).fetchall()


def list_leads_para_reativacao(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Leads em FU1/FU2/encerrado — candidatos a REATIVAÇÃO.

    Auditoria 2026-06-11 (HIGH): o polling era estritamente em_conversa,
    então lead que RESPONDIA ao follow-up (ou voltava depois do
    encerramento) era invisível — e ainda levava FU2/encerramento em
    cima da resposta. O poll cycle agora varre esses estados a cada
    tick comparando o hash do transcript; mensagem nova do lead →
    volta pra em_conversa.
    """
    return conn.execute(
        """
        SELECT * FROM leads
        WHERE estado IN (?, ?, ?)
        ORDER BY atualizado_em ASC
        """,
        (
            Estado.FOLLOW_UP_1_ENVIADO,
            Estado.FOLLOW_UP_2_ENVIADO,
            Estado.ENCERRADO_SEM_RESPOSTA,
        ),
    ).fetchall()


def list_leads_aguardando_humano(
    conn: sqlite3.Connection, limite: int | None = None,
) -> list[sqlite3.Row]:
    """Leads em AGUARDANDO_HUMANO — candidatos a RE-ENGAJE (P1 auditoria 24/jun).

    Antes AGUARDANDO_HUMANO era um buraco negro: o bot (único atendimento) nunca
    mais tocava o lead. Agora a FASE 0 do poll varre esses leads; se o lead manda
    mensagem nova, o bot reabre — exceto motivos terminais (opt-out, humano
    assumiu, etc.), checados via ``ultimo_motivo_transicao``.

    H2 (auditoria 24/jun): ``limite`` faz a sweep pegar só os N há mais tempo sem
    checar (``ah_checado_em`` ASC, NULLs primeiro), e o caller carimba cada um com
    ``marcar_ah_checado`` → round-robin com trabalho LIMITADO por tick, sem O(AH)
    chamadas get_conversation. ``None`` = sem limite (compat)."""
    sql = (
        "SELECT * FROM leads WHERE estado = ? "
        "ORDER BY ah_checado_em ASC"  # SQLite: NULL vem primeiro no ASC
    )
    params: tuple = (Estado.AGUARDANDO_HUMANO,)
    if limite is not None:
        sql += " LIMIT ?"
        params = (*params, limite)
    return conn.execute(sql, params).fetchall()


# --- Sweeper do pós-assinatura (#36, 25/jun) ---------------------------------

def list_contratos_pos_pendentes(
    conn: sqlite3.Connection, *, limite: int,
) -> list[sqlite3.Row]:
    """Contratos ASSINADOS com algum sub-passo do pós PENDENTE — candidatos ao
    sweep de retomada.

    ``pos_iniciado_em IS NOT NULL`` exclui contratos PRÉ-FEATURE (assinados antes
    do pós existir / com a flag off — têm tudo NULL e NÃO devem ser processados
    retroativamente). ``pos_travado_em IS NULL`` exclui os que já estouraram o
    teto de tentativas (Mario já alertado). Pendente = intake OU arquivo OU
    (tarefa E já tem person_id). Round-robin: menos tentados primeiro."""
    return conn.execute(
        "SELECT id, zapsign_doc_token, cliente_nome, pos_tentativas "
        "FROM contrato "
        "WHERE estado = 'ASSINADO' "
        "  AND pos_iniciado_em IS NOT NULL "
        "  AND pos_travado_em IS NULL "
        "  AND (intake_juridiq_em IS NULL "
        "       OR arquivo_pdf_em IS NULL "
        "       OR (tarefa_abertura_em IS NULL AND person_id IS NOT NULL)) "
        "ORDER BY pos_tentativas ASC, id ASC "
        "LIMIT ?",
        (limite,),
    ).fetchall()


def _update_contrato_best_effort(
    conn: sqlite3.Connection, sql: str, params: tuple,
) -> None:
    """UPDATE no contrato com retry curto sob 'database is locked' e, se
    persistir, ENGOLE (best-effort) — roda no scheduler; um lock AQUI não pode
    abortar o tick (pulando lembretes/follow-up). Erro não-lock propaga."""
    for tentativa in range(_MAX_RETRY_LOCK):
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if tentativa < _MAX_RETRY_LOCK - 1:
                time.sleep(_RETRY_LOCK_BASE * (2 ** tentativa))


def marcar_pos_iniciado(conn: sqlite3.Connection, contrato_id: int) -> None:
    """Carimba pos_iniciado_em na PRIMEIRA execução do pós (COALESCE → set-once,
    não sobrescreve). É o que torna o contrato elegível ao sweep — discrimina os
    pós-feature dos pré-existentes (que têm tudo NULL). BEST-EFFORT."""
    _update_contrato_best_effort(
        conn,
        "UPDATE contrato SET pos_iniciado_em = "
        "COALESCE(pos_iniciado_em, datetime('now')) WHERE id = ?",
        (contrato_id,),
    )


def registrar_tentativa_pos(conn: sqlite3.Connection, contrato_id: int) -> None:
    """Incrementa pos_tentativas (1 por passada do sweep). BEST-EFFORT: falhar =
    a tentativa não conta (re-tenta no próximo sweep, sem dano)."""
    _update_contrato_best_effort(
        conn,
        "UPDATE contrato SET pos_tentativas = pos_tentativas + 1 WHERE id = ?",
        (contrato_id,),
    )


def marcar_pos_travado(conn: sqlite3.Connection, contrato_id: int) -> None:
    """Marca pos_travado_em = now → o contrato SAI da fila do sweep (passo preso
    após o teto; o Mario foi alertado pra resolver à mão). BEST-EFFORT."""
    _update_contrato_best_effort(
        conn,
        "UPDATE contrato SET pos_travado_em = datetime('now') WHERE id = ?",
        (contrato_id,),
    )


def set_lead_email(conn: sqlite3.Connection, lead_id: int, email: str) -> None:
    """D4 (25/jun): persiste o email do lead (lowercase) pra casar reuniões
    marcadas FORA do bot com o lead certo. Idempotente — só grava se o valor
    mudou (o poll cycle chama isto sempre que vê um email no transcript, então
    evitar write à toa importa). Email vazio = no-op (não apaga).

    BEST-EFFORT no UPDATE (revisão adversarial 25/jun): roda no caminho QUENTE do
    poll (por-lead, por-tick); um 'database is locked' aqui NÃO pode abortar o
    ciclo (pularia lembretes/follow-up). Retry curto e, se persistir, engole — o
    email é re-gravado no próximo tick em que o lead mandar. Espelha o
    marcar_ah_checado (H2). O SELECT é seguro em WAL (readers não bloqueiam)."""
    norm = (email or "").strip().lower()
    if not norm:
        return
    row = conn.execute(
        "SELECT contato_email FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    if row is not None and (row["contato_email"] or "") == norm:
        return
    for tentativa in range(_MAX_RETRY_LOCK):
        try:
            conn.execute(
                "UPDATE leads SET contato_email = ?, atualizado_em = datetime('now') "
                "WHERE id = ?",
                (norm, lead_id),
            )
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if tentativa < _MAX_RETRY_LOCK - 1:
                time.sleep(_RETRY_LOCK_BASE * (2 ** tentativa))
    # Esgotou sob lock persistente — best-effort, não aborta o tick.


def lead_por_email(
    conn: sqlite3.Connection, email: str,
) -> list[sqlite3.Row]:
    """D4 (25/jun): leads cujo contato_email casa (lowercase). Lista porque, em
    tese, 2 leads podem ter o mesmo email — o caller trata ambiguidade."""
    norm = (email or "").strip().lower()
    if not norm:
        return []
    return conn.execute(
        "SELECT * FROM leads WHERE contato_email = ?", (norm,)
    ).fetchall()


def event_ids_de_reunioes(conn: sqlite3.Connection) -> set[str]:
    """D4 (25/jun): todos os ``reuniao_event_id`` não-nulos — eventos do Calendar
    JÁ rastreados pelo bot. A sync de reunião manual pula esses (auto-dedup: após
    auto-vincular, o event_id entra aqui e não reprocessa)."""
    rows = conn.execute(
        "SELECT reuniao_event_id FROM leads "
        "WHERE reuniao_event_id IS NOT NULL AND reuniao_event_id != ''"
    ).fetchall()
    return {r["reuniao_event_id"] for r in rows}


def evento_manual_ja_alertado(
    conn: sqlite3.Connection, event_id: str,
) -> bool:
    """D4 (25/jun): True se já avisamos o Mario sobre este evento do Calendar
    (não-rastreado/conflito) — pra o aviso sair 1× por evento, não a cada tick."""
    if not event_id:
        return True  # sem id não dá pra deduplicar → não alerta (evita spam)
    row = conn.execute(
        "SELECT 1 FROM eventos_manuais_alertados WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    return row is not None


def marcar_evento_manual_alertado(
    conn: sqlite3.Connection, event_id: str,
) -> None:
    """D4 (25/jun): registra que o Mario já foi avisado deste evento. Idempotente."""
    if not event_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO eventos_manuais_alertados (event_id) VALUES (?)",
        (event_id,),
    )


def marcar_ah_checado(conn: sqlite3.Connection, lead_id: int) -> None:
    """H2 (auditoria 24/jun): carimba ah_checado_em = now → rotaciona o lead pro
    fim da fila da sweep de re-engaje. NÃO toca atualizado_em (que é 'última
    modificação de negócio').

    BEST-EFFORT (revisão adversarial 24/jun): roda até 25×/tick no topo da sweep.
    Se colidir com 'database is locked' (web×scheduler = processos separados),
    re-tenta curto e, se persistir, ENGOLE — o carimbo é só uma marca de rotação;
    falhar significa só que o lead é re-checado no tick seguinte (rotação
    levemente menos justa, sem dano). NUNCA propaga: senão um lock AQUI abortaria
    o tick inteiro (pulando lembretes/follow-up), justo o que o H2 evita. Erro
    inesperado (não-lock) propaga normalmente."""
    for tentativa in range(_MAX_RETRY_LOCK):
        try:
            conn.execute(
                "UPDATE leads SET ah_checado_em = datetime('now') WHERE id = ?",
                (lead_id,),
            )
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            if tentativa < _MAX_RETRY_LOCK - 1:
                time.sleep(_RETRY_LOCK_BASE * (2 ** tentativa))
    # Esgotou o retry sob lock persistente — best-effort, não aborta o tick.


def ultimo_motivo_transicao(
    conn: sqlite3.Connection, lead_id: int,
) -> str | None:
    """Motivo da transição MAIS RECENTE do lead (None se não houver).

    Usado pra decidir se um lead em AGUARDANDO_HUMANO pode reabrir quando volta a
    falar — motivos terminais (opt_out, humano_assumiu_conversa) nunca reabrem."""
    row = conn.execute(
        "SELECT motivo FROM transicoes WHERE lead_id = ? ORDER BY id DESC LIMIT 1",
        (lead_id,),
    ).fetchone()
    return row["motivo"] if row else None


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
        reuniao_dt = datetime.fromisoformat(reuniao_em_iso)
        if reuniao_dt.tzinfo is None:
            # ISO naive: interpreta como horário de Brasília (tz da
            # agenda) — astimezone() direto em naive assumiria o tz do
            # SO (UTC no VPS) e deslocaria os lembretes 3h.
            from zoneinfo import ZoneInfo
            reuniao_dt = reuniao_dt.replace(tzinfo=ZoneInfo("America/Sao_Paulo"))
        reuniao_dt = reuniao_dt.astimezone(UTC)
    except ValueError as exc:
        # D5 (auditoria 24/jun): ISO inparseável NÃO pode virar reunião
        # fantasma. Antes caía em ``reuniao_dt = now`` → delta≈0 marcava TODOS
        # os lembretes como já enviados (pré-supressão) E gravava o lixo em
        # reuniao_em, que o reminder_cycle pulava pra sempre (nunca limpava).
        # O caller já normaliza (start.isoformat()), então isso só dispara em
        # bug real — falha alto pra não persistir reunião quebrada.
        raise ValueError(
            f"reuniao_em_iso inválido em set_reuniao: {reuniao_em_iso!r}"
        ) from exc
    delta = reuniao_dt - now

    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    lembrete_24h_sent = now_str if delta < timedelta(hours=24) else None
    lembrete_2h_sent = now_str if delta < timedelta(hours=2) else None
    lembrete_30min_sent = now_str if delta < timedelta(minutes=30) else None
    lembrete_5min_sent = now_str if delta < timedelta(minutes=5) else None

    conn.execute(
        """
        UPDATE leads SET
            reuniao_em = ?,
            reuniao_event_id = ?,
            reuniao_meet_link = ?,
            lembrete_24h_enviado_em = ?,
            lembrete_2h_enviado_em = ?,
            lembrete_30min_enviado_em = ?,
            lembrete_5min_enviado_em = ?,
            noshow_token = NULL,
            atualizado_em = datetime('now')
        WHERE id = ?
        """,
        (
            reuniao_em_iso, event_id, meet_link,
            lembrete_24h_sent, lembrete_2h_sent, lembrete_30min_sent,
            lembrete_5min_sent,
            lead_id,
        ),
    )


def lead_com_reuniao_no_horario(
    conn: sqlite3.Connection, reuniao_em_iso: str, exclude_lead_id: int,
) -> int | None:
    """D2 (auditoria 24/jun): retorna o id de OUTRO lead que já tem reunião no
    MESMO instante, ou ``None``.

    Usado pra barrar double-booking no confirmar — o ``find_available_slots``
    só lê o freeBusy do Calendar (eventual-consistente), nunca o DB, então dois
    leads conseguiriam confirmar o mesmo slot. ``reuniao_em`` só fica não-nulo
    enquanto a reunião está ativa (``clear_reuniao`` zera no cancel/remarcação)
    e só reuniões futuras importam, então a varredura é de poucas linhas.

    Compara por INSTANTE (parse pra UTC), não por string crua: o caminho LLM do
    confirmar pode gravar o mesmo momento com offset diferente (ex.: a Julia
    ecoa ``+00:00`` em vez de ``-03:00``), e a igualdade textual deixaria o
    double-booking passar (fail-open). Fantasmas com ISO inparseável são
    ignorados aqui — o reminder_cycle (D5) os limpa.
    """
    try:
        alvo = datetime.fromisoformat(reuniao_em_iso).astimezone(UTC)
    except (ValueError, TypeError):
        return None  # alvo inparseável — o caller já normaliza; nada a casar
    rows = conn.execute(
        "SELECT id, reuniao_em FROM leads "
        "WHERE reuniao_em IS NOT NULL AND id != ?",
        (exclude_lead_id,),
    ).fetchall()
    for row in rows:
        try:
            outro = datetime.fromisoformat(row["reuniao_em"]).astimezone(UTC)
        except (ValueError, TypeError):
            continue
        if outro == alvo:
            return row["id"]
    return None


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
            lembrete_5min_enviado_em = NULL,
            noshow_token = NULL,
            atualizado_em = datetime('now')
        WHERE id = ?
        """,
        (lead_id,),
    )


def mark_lembrete_enviado(
    conn: sqlite3.Connection, lead_id: int, lembrete: str,
) -> None:
    """``lembrete`` ∈ {'24h', '2h', '30min', '5min'}. Idempotente."""
    coluna_map = {
        "24h": "lembrete_24h_enviado_em",
        "2h": "lembrete_2h_enviado_em",
        "30min": "lembrete_30min_enviado_em",
        "5min": "lembrete_5min_enviado_em",
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


def marcar_noshow_avisado(
    conn: sqlite3.Connection, lead_id: int, token: str,
) -> None:
    """Grava o token do link de cancelamento de no-show (= ping já enviado)."""
    conn.execute(
        "UPDATE leads SET noshow_token = ?, atualizado_em = datetime('now') "
        "WHERE id = ?",
        (token, lead_id),
    )


def get_lead_by_noshow_token(
    conn: sqlite3.Connection, token: str,
) -> sqlite3.Row | None:
    """Lead pelo token do link de cancelamento de no-show (None se inválido)."""
    if not token:
        return None
    return conn.execute(
        "SELECT * FROM leads WHERE noshow_token = ?", (token,),
    ).fetchone()


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
