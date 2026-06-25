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
    """Lead VENCIDO pro followup, no contrato novo (auditoria 2026-06-11):

    - em_conversa: relógio próprio = ultima_msg_lead_em (>48h atrás)
    - FU1/FU2: proxima_acao_em no passado
    Setamos os dois pra cobrir qualquer estado pedido.
    """
    past_clock = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=1)
    ).strftime("%Y-%m-%d %H:%M:%S")
    idle_50h = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=50)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, proxima_acao_em, ultima_msg_lead_em)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (jurichat_lead_id, conversation_id, "5511...", "Test", estado,
         past_clock, idle_50h),
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
    fake_jurichat.start_human_support = AsyncMock(return_value={"success": True})
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
    # FU1 agora também busca a conversa (Signal 0 do C2, auditoria 24/jun).
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: oi (há 2 dias)",
    })
    fake_jurichat.start_human_support = AsyncMock(return_value={"success": True})
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
    # Contrato novo (auditoria 2026-06-11): tag de exclusão = humano
    # cuida → AGUARDANDO_HUMANO (antes só limpava o relógio, e com o
    # critério por ultima_msg o lead voltaria todo tick).
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["erro_atual"] == "excluido_followup_etiqueta"
    assert lead["proxima_acao_em"] is None
    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_flags_lead_when_get_tags_fails(db_conn):
    """get_lead_tags raises (network down, 5xx exhausted) → register_error
    + continue. State unchanged so lead is retried next tick."""
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(side_effect=RuntimeError("boom"))
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
    assert lead["erro_atual"] == "jurichat_get_tags_failed"
    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_flags_lead_when_dispatch_step_fails(db_conn):
    """Send raises mid-dispatch → register_error('scheduler_step_failed').

    Critical: this proves the new atomic transicao+schedule contract.
    Because we transition+reschedule BEFORE sending, a send failure here
    leaves the lead in FU1 with proxima_acao_em set 72h ahead — so the
    lead WILL NOT be picked up on the next tick (no double-send risk)."""
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: oi",
    })
    fake_jurichat.send_message = AsyncMock(side_effect=RuntimeError("send_down"))

    async def fake_followup_gen(**kwargs):
        return "Oi! Conseguiu olhar?"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    # The transition committed before the send attempt → lead is FU1.
    assert lead["estado"] == Estado.FOLLOW_UP_1_ENVIADO
    # And proxima_acao_em is now 72h in the future, so next tick won't
    # re-dispatch and double-send.
    assert lead["proxima_acao_em"] is not None
    # Error flagged so Mario can audit.
    assert lead["erro_atual"] == "scheduler_step_failed"


@pytest.mark.asyncio
async def test_cycle_pula_lead_suprimido_opt_out(db_conn):
    """E1 (auditoria 24/jun): o ciclo de follow-up (sender de maior volume) NÃO
    pode mandar mensagem pra quem está na lista de supressão (opt-out LGPD).
    Pula ANTES de qualquer chamada ao Jurichat e encerra em AGUARDANDO_HUMANO."""
    from noviello_funil.opt_out import registrar_opt_out
    from noviello_funil.state import ultimo_motivo_transicao

    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)
    db_conn.execute(
        "UPDATE leads SET contato_telefone=? WHERE jurichat_conversation_id='C-1'",
        ("5511988887777",),
    )
    # O lead já pediu opt-out antes (telefone na supressão).
    registrar_opt_out(db_conn, telefone="5511988887777", motivo="teste")

    fake_jurichat = MagicMock()
    fake_jurichat.get_lead_tags = AsyncMock(return_value=[])
    fake_jurichat.send_message = AsyncMock()

    async def fake_followup_gen(**kwargs):
        raise AssertionError("não pode gerar follow-up pra lead suprimido")

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        gerar_followup_msg=fake_followup_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert ultimo_motivo_transicao(db_conn, lead["id"]) == "opt_out"
    # Não enviou nada e nem consultou o Jurichat (pulou cedo).
    fake_jurichat.send_message.assert_not_awaited()
    fake_jurichat.get_lead_tags.assert_not_awaited()


@pytest.mark.asyncio
async def test_cycle_nao_manda_followup_pra_lead_com_reuniao(db_conn):
    """HIGH da auditoria: lead com reunião marcada recebia FU1 ('percebi
    que talvez não seja o momento') junto com lembretes da reunião."""
    _make_due_lead(db_conn, "L-1", "C-1", Estado.EM_CONVERSA)
    db_conn.execute(
        "UPDATE leads SET reuniao_em='2027-06-15T15:00:00-03:00' "
        "WHERE jurichat_conversation_id='C-1'"
    )

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
    assert lead["estado"] == Estado.EM_CONVERSA  # intocado
    fake_jurichat.send_message.assert_not_awaited()
