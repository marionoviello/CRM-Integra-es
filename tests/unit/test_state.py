"""Tests for the state repository layer."""

import pytest

from noviello_funil.state import (
    Estado,
    create_lead_if_absent,
    get_lead_by_conversation,
    is_webhook_processed,
    mark_webhook_processed,
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
