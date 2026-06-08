"""Integration tests for the follow-up scheduler."""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.scheduler import (
    is_eligible_for_followup,
    run_followup_cycle,
)
from noviello_funil.state import (
    Estado,
    get_lead_by_conversation,
)


def _make_due_lead(conn, jurichat_lead_id, conversation_id, estado):
    past = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, proxima_acao_em)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (jurichat_lead_id, conversation_id, "5511...", "Test", estado, past),
    )


# --- Eligibility rule -----------------------------------------------------

def test_eligible_when_no_tags():
    assert is_eligible_for_followup([]) is True


def test_eligible_when_fazer_followup_present():
    assert is_eligible_for_followup(["Fazer Follow up"]) is True


def test_eligible_when_proposta_enviada_present():
    assert is_eligible_for_followup(["Proposta enviada"]) is True


def test_eligible_when_optin_combined_with_exclusion():
    # opt-in wins
    assert is_eligible_for_followup(["Pagamento pendente", "Fazer Follow up"]) is True


def test_not_eligible_with_only_exclusion_tags():
    assert is_eligible_for_followup(["Cliente Ativo"]) is False
    assert is_eligible_for_followup(["Pagamento pendente", "Reunião marcada"]) is False
    assert is_eligible_for_followup(["Desqualificado"]) is False


# --- Full cycle -----------------------------------------------------------

@pytest.mark.asyncio
async def test_cycle_sends_first_followup_when_in_em_conversa(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: oi (há 2 dias)",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_followup_gen(**kwargs):
        return "Oi! Conseguiu olhar aquilo que falamos?"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.FOLLOW_UP_1_ENVIADO
    fake_jurichat.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_cycle_sends_second_followup_when_in_follow_up_1(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.FOLLOW_UP_1_ENVIADO)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_followup_gen(**kwargs):
        raise AssertionError("should not call brain on followup_2")

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.FOLLOW_UP_2_ENVIADO
    sent_text = fake_jurichat.send_message.call_args[0][1]
    assert "encerrar" in sent_text.lower()


@pytest.mark.asyncio
async def test_cycle_closes_silently_when_in_follow_up_2(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.FOLLOW_UP_2_ENVIADO)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.send_message = AsyncMock()

    async def fake_followup_gen(**kwargs):
        return "x"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.ENCERRADO_SEM_RESPOSTA
    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_skips_lead_with_excluding_tag(db_conn):
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=["Cliente Ativo"])
    fake_jurichat.send_message = AsyncMock()

    async def fake_followup_gen(**kwargs):
        return "x"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # unchanged
    assert lead["erro_atual"] == "excluido_followup_etiqueta"
    assert lead["proxima_acao_em"] is None
    fake_jurichat.send_message.assert_not_awaited()
