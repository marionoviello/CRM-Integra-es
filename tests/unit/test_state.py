"""Tests for the state repository layer."""

import pytest

from noviello_funil.state import (
    Estado,
    bump_turnos,
    create_lead_if_absent,
    get_lead_by_conversation,
    is_webhook_processed,
    list_leads_vencidos,
    mark_webhook_processed,
    record_lead_message_received,
    register_error,
    transicao,
)


def test_create_lead_if_absent_creates_new(db_conn):
    lead = create_lead_if_absent(
        db_conn,
        jurichat_lead_id="L-1",
        jurichat_conversation_id="C-1",
        contato_telefone="5511999999999",
        contato_nome="Maria",
    )
    assert lead["id"] is not None
    assert lead["jurichat_lead_id"] == "L-1"
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["turnos"] == 0


def test_create_lead_if_absent_returns_existing(db_conn):
    first = create_lead_if_absent(
        db_conn, "L-1", "C-1", "5511...", "Maria"
    )
    second = create_lead_if_absent(
        db_conn, "L-1", "C-1", "5511...", "Maria"
    )
    assert first["id"] == second["id"]


def test_get_lead_by_conversation_returns_none_when_absent(db_conn):
    assert get_lead_by_conversation(db_conn, "C-NONE") is None


def test_get_lead_by_conversation_returns_lead(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead is not None
    assert lead["jurichat_lead_id"] == "L-1"


def test_webhook_idempotency_first_time_returns_false(db_conn):
    assert is_webhook_processed(db_conn, "jurichat", "evt-1") is False


def test_webhook_idempotency_after_marking_returns_true(db_conn):
    mark_webhook_processed(db_conn, "jurichat", "evt-1", "hash-1")
    assert is_webhook_processed(db_conn, "jurichat", "evt-1") is True


def test_webhook_marking_is_idempotent_no_raise(db_conn):
    mark_webhook_processed(db_conn, "jurichat", "evt-1", "hash-1")
    # Re-marking the same event must not raise
    mark_webhook_processed(db_conn, "jurichat", "evt-1", "hash-1")


def test_estado_constants():
    # All 5 spec'd states exist as constants (spec §5)
    assert Estado.EM_CONVERSA == "em_conversa"
    assert Estado.FOLLOW_UP_1_ENVIADO == "follow_up_1_enviado"
    assert Estado.FOLLOW_UP_2_ENVIADO == "follow_up_2_enviado"
    assert Estado.ENCERRADO_SEM_RESPOSTA == "encerrado_sem_resposta"
    assert Estado.AGUARDANDO_HUMANO == "aguardando_humano"


def test_transicao_updates_estado_and_logs(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    transicao(
        db_conn, lead["id"], Estado.AGUARDANDO_HUMANO,
        motivo="claude_propor", payload={"acao": "propor"},
    )

    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["estado"] == Estado.AGUARDANDO_HUMANO

    transicoes = db_conn.execute(
        "SELECT * FROM transicoes WHERE lead_id = ?", (lead["id"],)
    ).fetchall()
    assert len(transicoes) == 1
    assert transicoes[0]["estado_anterior"] == Estado.EM_CONVERSA
    assert transicoes[0]["estado_novo"] == Estado.AGUARDANDO_HUMANO
    assert transicoes[0]["motivo"] == "claude_propor"


def test_bump_turnos_increments(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    bump_turnos(db_conn, lead["id"])
    bump_turnos(db_conn, lead["id"])
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["turnos"] == 2


def test_record_lead_message_received_updates_timestamp(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    record_lead_message_received(db_conn, lead["id"], proxima_acao_horas=48)
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["ultima_msg_lead_em"] is not None
    assert updated["proxima_acao_em"] is not None


def test_record_lead_message_resets_turnos_if_reopening(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    bump_turnos(db_conn, lead["id"])
    bump_turnos(db_conn, lead["id"])
    transicao(db_conn, lead["id"], Estado.ENCERRADO_SEM_RESPOSTA, motivo="timer")

    # Lead reopens — turnos must reset
    record_lead_message_received(
        db_conn, lead["id"], proxima_acao_horas=48, reset_turnos=True,
    )
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["turnos"] == 0


def test_register_error_sets_flag(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    register_error(db_conn, lead["id"], "claude_invalid_json")
    updated = get_lead_by_conversation(db_conn, "C-1")
    assert updated["erro_atual"] == "claude_invalid_json"


def test_list_leads_vencidos_returns_only_due_and_active(db_conn):
    import datetime

    past = (datetime.datetime.utcnow() - datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    future = (datetime.datetime.utcnow() + datetime.timedelta(hours=1)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

    # Lead 1: due, em_conversa → should appear
    conn = db_conn
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
                              contato_telefone, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?)""",
        ("L-due", "C-due", "5511...", Estado.EM_CONVERSA, past),
    )
    # Lead 2: not yet due
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
                              contato_telefone, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?)""",
        ("L-future", "C-future", "5511...", Estado.EM_CONVERSA, future),
    )
    # Lead 3: due but in terminal state → should NOT appear
    conn.execute(
        """INSERT INTO leads (jurichat_lead_id, jurichat_conversation_id,
                              contato_telefone, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?)""",
        ("L-handed", "C-handed", "5511...", Estado.AGUARDANDO_HUMANO, past),
    )

    vencidos = list_leads_vencidos(conn)
    ids = {row["jurichat_lead_id"] for row in vencidos}
    assert "L-due" in ids
    assert "L-future" not in ids
    assert "L-handed" not in ids
