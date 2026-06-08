"""Integration tests for the polling cycle (run_poll_cycle).

Covers:
  * hash unchanged → no Claude call, just reschedule
  * hash changed + responder/propor/handoff
  * last line "Atendente:" → Mario assumed
  * max_turnos cap
  * DecisaoInvalida → register_error + retry next tick
  * get_conversation failure
  * recent-activity carve-out: polling picks an active em_conversa lead,
    the follow-up cycle does NOT touch it.
"""

import datetime
import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.scheduler import run_followup_cycle, run_poll_cycle
from noviello_funil.state import Estado, get_lead_by_conversation

# --- Test helpers ---------------------------------------------------------

def _insert_lead_due_for_poll(
    conn,
    *,
    jurichat_lead_id: str = "L-1",
    conversation_id: str = "C-1",
    transcript_hash: str | None = None,
    ultima_msg_lead_em: str | None = None,
):
    """Insert an em_conversa lead with proxima_acao_em in the past."""
    past = (
        datetime.datetime.utcnow() - datetime.timedelta(seconds=10)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, proxima_acao_em, ultimo_transcript_hash,
            ultima_msg_lead_em)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            jurichat_lead_id, conversation_id, "5511999999999", "Maria",
            Estado.EM_CONVERSA, past, transcript_hash, ultima_msg_lead_em,
        ),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_jurichat(transcript: str):
    """Fake JurichatClient with stubbed get_conversation + send_message."""
    fake = MagicMock()
    fake.get_conversation = AsyncMock(return_value={"transcription": transcript})
    fake.send_message = AsyncMock(return_value={"id": "msg-1"})
    return fake


async def _triagem_returning(decisao: Decisao):
    """Build a triagem_fn that always returns the given decisao."""
    async def _fn(**kwargs):
        return decisao
    return _fn


# --- Cases ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_hash_unchanged_no_claude_call_just_reschedule(db_conn):
    transcript = "Lead: oi tudo bem"
    _insert_lead_due_for_poll(db_conn, transcript_hash=_sha(transcript))

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["proxima_acao_em"] is not None
    # Hash unchanged → no triagem, no send
    triagem_fn.assert_not_called()
    jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_hash_changed_responder_sends_and_reschedules(db_conn):
    transcript = "Lead: olá\nAtendente: oi\nLead: quero saber sobre planos"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale-hash")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Claro, posso ajudar!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["ultimo_transcript_hash"] == _sha(transcript)
    assert lead["proxima_acao_em"] is not None
    jurichat.send_message.assert_awaited_once_with("C-1", "Claro, posso ajudar!")


@pytest.mark.asyncio
async def test_hash_changed_propor_transitions_and_notifies(db_conn):
    # NB: last line MUST be a Lead line, otherwise the "Mario assumed"
    # detector trips before we get to the Claude decision.
    transcript = "Atendente: ótimo\nLead: quero contratar agora"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="propor",
            mensagem="Vou te passar para o Mario.",
            resumo_caso="Cliente quer plano familiar.",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["proxima_acao_em"] is None
    assert lead["ultimo_transcript_hash"] == _sha(transcript)
    # Two sends: the closing message to lead + notification to Mario.
    assert jurichat.send_message.await_count == 2
    sent_conv_ids = [call.args[0] for call in jurichat.send_message.await_args_list]
    assert "C-1" in sent_conv_ids
    assert "mario-conv" in sent_conv_ids


@pytest.mark.asyncio
async def test_hash_changed_handoff_transitions_and_notifies(db_conn):
    transcript = "Lead: preciso falar com humano urgente"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="handoff",
            mensagem="(unused for handoff)",
            motivo_handoff="Lead pediu humano",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["proxima_acao_em"] is None
    assert lead["ultimo_transcript_hash"] == _sha(transcript)
    # Only the notify_mario send, no message to the lead on handoff.
    jurichat.send_message.assert_awaited_once()
    assert jurichat.send_message.await_args.args[0] == "mario-conv"


@pytest.mark.asyncio
async def test_last_line_atendente_assumes_mario_no_claude_call(db_conn):
    transcript = "Lead: oi\nAtendente: eu assumo daqui (Mario)"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["proxima_acao_em"] is None
    assert lead["ultimo_transcript_hash"] == _sha(transcript)
    triagem_fn.assert_not_called()
    jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_turnos_reached_notifies_and_handoff(db_conn):
    # 20 Lead: lines triggers cap when max_turnos = 20.
    lines = [f"Lead: msg {i}" for i in range(20)]
    transcript = "\n".join(lines)
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["proxima_acao_em"] is None
    assert lead["ultimo_transcript_hash"] == _sha(transcript)
    # Notification to Mario fired
    jurichat.send_message.assert_awaited_once()
    assert jurichat.send_message.await_args.args[0] == "mario-conv"


@pytest.mark.asyncio
async def test_decisao_invalida_registers_error_keeps_hash(db_conn):
    transcript = "Lead: oi\nLead: alguém?"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)

    async def bad_triagem(**kwargs):
        raise DecisaoInvalida("bad json")

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=bad_triagem,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["erro_atual"] == "claude_invalid_json"
    # Hash NOT updated → next tick will retry with the same content.
    assert lead["ultimo_transcript_hash"] == "stale"
    # Lead rescheduled for retry.
    assert lead["proxima_acao_em"] is not None
    # Notification to Mario fired (only one send).
    jurichat.send_message.assert_awaited_once()
    assert jurichat.send_message.await_args.args[0] == "mario-conv"


@pytest.mark.asyncio
async def test_get_conversation_failure_registers_error_and_reschedules(db_conn):
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = MagicMock()
    jurichat.get_conversation = AsyncMock(side_effect=RuntimeError("api down"))
    jurichat.send_message = AsyncMock()
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["erro_atual"] == "jurichat_get_conversation_failed"
    assert lead["proxima_acao_em"] is not None  # rescheduled for retry
    triagem_fn.assert_not_called()


@pytest.mark.asyncio
async def test_active_em_conversa_picked_by_poll_not_by_followup(db_conn):
    """The recent-activity carve-out: an em_conversa lead with
    ultima_msg_lead_em < 24h ago is owned by the polling cycle. The
    follow-up cycle must skip it (otherwise it would fire a follow-up
    nudge on every active conversation)."""
    # Lead with activity 10 minutes ago — clearly "active".
    recent = (
        datetime.datetime.utcnow() - datetime.timedelta(minutes=10)
    ).strftime("%Y-%m-%d %H:%M:%S")
    _insert_lead_due_for_poll(db_conn, ultima_msg_lead_em=recent)

    # The follow-up cycle's queue must NOT include this lead.
    from noviello_funil.state import (
        list_leads_para_polling,
        list_leads_vencidos,
    )
    assert len(list_leads_vencidos(db_conn)) == 0
    assert len(list_leads_para_polling(db_conn)) == 1

    # And just to prove it end-to-end: run the follow-up cycle and confirm
    # no Jurichat calls happen.
    jurichat = MagicMock()
    jurichat.get_lead_tags = AsyncMock()
    jurichat.get_conversation = AsyncMock()
    jurichat.send_message = AsyncMock()

    async def fake_gen(**kwargs):
        return "x"

    await run_followup_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        gerar_followup_msg=fake_gen,
        followup_2_apos_horas=72,
        encerramento_apos_horas=24,
    )
    jurichat.get_lead_tags.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_em_conversa_still_picked_by_followup(db_conn):
    """Sanity check the other side of the carve-out: a lead in em_conversa
    that has been idle > 24h (or has no recorded activity) IS picked up
    by the follow-up cycle."""
    # No ultima_msg_lead_em recorded → counts as idle → follow-up picks it.
    _insert_lead_due_for_poll(db_conn, ultima_msg_lead_em=None)

    from noviello_funil.state import list_leads_vencidos
    assert len(list_leads_vencidos(db_conn)) == 1


@pytest.mark.asyncio
async def test_poll_marks_lead_activity_when_hash_changes(db_conn):
    """When new content lands, the poll cycle must stamp ultima_msg_lead_em
    so the follow-up cycle's idle carve-out continues to treat the lead
    as active on subsequent ticks."""
    transcript = "Lead: nova mensagem"
    _insert_lead_due_for_poll(
        db_conn, transcript_hash="stale", ultima_msg_lead_em=None,
    )

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Ok")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["ultima_msg_lead_em"] is not None
