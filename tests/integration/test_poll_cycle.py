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
from zoneinfo import ZoneInfo

import httpx
import pytest

from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.calendar_client import Slot
from noviello_funil.outbound import JurichatClient
from noviello_funil.scheduler import (
    CalendarConfig,
    run_followup_cycle,
    run_poll_cycle,
    sync_jurichat_conversations,
)
from noviello_funil.state import (
    Estado,
    get_horarios_oferecidos,
    get_lead_by_conversation,
    set_horarios_oferecidos,
)

# --- Test helpers ---------------------------------------------------------

def _insert_lead_due_for_poll(
    conn,
    *,
    jurichat_lead_id: str = "L-1",
    conversation_id: str = "C-1",
    transcript_hash: str | None = None,
    ultima_msg_lead_em: str | None = None,
    turnos: int = 0,
):
    """Insert an em_conversa lead with proxima_acao_em in the past."""
    past = (
        datetime.datetime.utcnow() - datetime.timedelta(seconds=10)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, proxima_acao_em, ultimo_transcript_hash,
            ultima_msg_lead_em, turnos)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            jurichat_lead_id, conversation_id, "5511999999999", "Maria",
            Estado.EM_CONVERSA, past, transcript_hash, ultima_msg_lead_em,
            turnos,
        ),
    )


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _make_jurichat(transcript: str):
    """Fake JurichatClient with stubbed get_conversation + send_message.

    Inclui ``start_human_support`` (idempotente, retorna sucesso) porque o
    novo contrato exige essa chamada antes de cada send_message + notify_mario.
    """
    fake = MagicMock()
    fake.get_conversation = AsyncMock(return_value={"transcription": transcript})
    fake.send_message = AsyncMock(return_value={"id": "msg-1"})
    fake.start_human_support = AsyncMock(return_value={"success": True})
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
async def test_responder_com_promessa_de_resultado_bloqueia_e_handoff(db_conn):
    """E3 (auditoria 24/jun): se o texto do modelo prometer resultado (OAB Prov.
    205/2021), o bot NÃO manda ao lead — envia msg neutra, passa pro humano
    (AGUARDANDO_HUMANO) e alerta o Mario com o trecho barrado."""
    transcript = "Lead: vou ganhar essa causa?\nAtendente: oi\nLead: vou?"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale-hash")

    jurichat = _make_jurichat(transcript)
    promessa = "Pode ficar tranquilo, garanto o êxito da sua ação!"
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem=promessa)
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    # Passou pro humano e marcou o erro.
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["erro_atual"] == "promessa_resultado_bloqueada"
    # O texto com promessa NUNCA foi enviado a ninguém.
    enviados = [c.args[1] for c in jurichat.send_message.call_args_list]
    assert promessa not in enviados
    # O lead recebeu uma msg neutra (sem promessa).
    para_lead = [c.args[1] for c in jurichat.send_message.call_args_list if c.args[0] == "C-1"]
    assert len(para_lead) == 1
    assert "equipe" in para_lead[0].lower() or "retorno" in para_lead[0].lower()
    # O Mario foi alertado com o trecho barrado.
    para_mario = [c.args[1] for c in jurichat.send_message.call_args_list if c.args[0] == "mario-conv"]
    assert len(para_mario) == 1
    assert "promessa de resultado" in para_mario[0].lower()


@pytest.mark.asyncio
async def test_promessa_de_resultado_bloqueia_tambem_no_confirmar_horario(db_conn):
    """E3 (revisão adversarial 24/jun): o backstop OAB roda ANTES do dispatch,
    então cobre TODOS os ramos que mandam prosa do modelo — não só o responder.
    Promessa num confirmar_horario (lead quente) também é barrada + handoff."""
    transcript = (
        "Lead: jose@exemplo.com\n"
        "Atendente: Tenho ter 14h\n"
        "Lead: a terça 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    promessa = "Agendado! Pode ficar tranquilo, garanto o êxito da sua causa."
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem=promessa,
            horario_escolhido_iso="2027-06-08T14:00:00-03:00",
            lead_email="jose@exemplo.com",
            resumo_caso="Inventário SP",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["erro_atual"] == "promessa_resultado_bloqueada"
    # NÃO criou evento nem mandou a promessa (bloqueou antes do dispatch).
    calendar_client.create_event.assert_not_awaited()
    enviados = [c.args[1] for c in jurichat.send_message.call_args_list]
    assert promessa not in enviados


@pytest.mark.asyncio
async def test_poll_cycle_alerta_mario_sobre_lead_preso_uma_vez(db_conn):
    """F1 (auditoria 24/jun): lead com >= 3 falhas consecutivas → o ciclo alerta
    o Mario UMA vez ao fim do tick (antes erro_atual era write-only e o lead
    ficava mudo/invisível). Não re-alerta no tick seguinte."""
    from noviello_funil.state import create_lead_if_absent, register_error

    # Lead NÃO due (proxima_acao_em NULL) com 3 erros consecutivos acumulados.
    lead = create_lead_if_absent(db_conn, "L-preso", "C-preso", "5511...", "Preso")
    for _ in range(3):
        register_error(db_conn, lead["id"], "jurichat_send_failed")

    triagem_fn = await _triagem_returning(Decisao(acao="responder", mensagem="x"))
    jurichat = _make_jurichat("Lead: oi")
    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )
    para_mario = [
        c for c in jurichat.send_message.call_args_list if c.args[0] == "mario-conv"
    ]
    assert len(para_mario) == 1
    assert "preso" in para_mario[0].args[1].lower()
    row = get_lead_by_conversation(db_conn, "C-preso")
    assert row["erro_alertado_em"] is not None

    # 2º tick: NÃO re-alerta (já carimbado).
    jurichat2 = _make_jurichat("Lead: oi")
    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat2, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )
    para_mario2 = [
        c for c in jurichat2.send_message.call_args_list if c.args[0] == "mario-conv"
    ]
    assert para_mario2 == []


@pytest.mark.asyncio
async def test_oferecer_horarios_erro_transitorio_calendar_re_tenta(db_conn):
    """F2 (auditoria 24/jun): GoogleCalendarError (transitório: timeout/5xx) →
    re-tenta no próximo tick, SEM handoff nem alerta imediato."""
    from noviello_funil.calendar_client import GoogleCalendarError

    transcript = "Atendente: Oi\nLead: Quero agendar"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    calendar_client.find_available_slots = AsyncMock(
        side_effect=GoogleCalendarError("503 Service Unavailable")
    )
    triagem_fn = await _triagem_returning(
        Decisao(acao="oferecer_horarios", mensagem="Horários: {{HORARIOS}}")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # segue, re-tenta
    assert lead["erro_atual"] == "calendar_find_slots_failed"
    assert lead["proxima_acao_em"] is not None
    # Transitório: NÃO alerta o Mario (1 falha < limiar do sweep F1).
    para_mario = [
        c for c in jurichat.send_message.call_args_list if c.args[0] == "mario-conv"
    ]
    assert para_mario == []


@pytest.mark.asyncio
async def test_oferecer_horarios_erro_httpx_e_transitorio_re_tenta(db_conn):
    """F2 (revisão própria 24/jun): o find_available_slots NÃO envolve o httpx em
    GoogleCalendarError (raise_for_status/timeout vazam crus). httpx.HTTPError é
    TRANSITÓRIO — re-tenta, NÃO faz handoff (senão um 503 viraria handoff)."""
    import httpx

    transcript = "Atendente: Oi\nLead: Quero agendar"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    calendar_client.find_available_slots = AsyncMock(
        side_effect=httpx.ReadTimeout("calendar timeout")
    )
    triagem_fn = await _triagem_returning(
        Decisao(acao="oferecer_horarios", mensagem="Horários: {{HORARIOS}}")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # re-tenta, não handoff
    assert lead["erro_atual"] == "calendar_find_slots_failed"


@pytest.mark.asyncio
async def test_oferecer_horarios_erro_inesperado_handoff_e_alerta(db_conn):
    """F2 (auditoria 24/jun): erro INESPERADO (bug determinístico, ex.: regressão
    pós-deploy) NÃO fica em reschedule mudo infinito — degrada pra handoff +
    alerta o Mario."""
    transcript = "Atendente: Oi\nLead: Quero agendar"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    calendar_client.find_available_slots = AsyncMock(
        side_effect=TypeError("regressão: payload mudou")
    )
    triagem_fn = await _triagem_returning(
        Decisao(acao="oferecer_horarios", mensagem="Horários: {{HORARIOS}}")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO  # handoff, não loop mudo
    assert lead["erro_atual"] == "calendar_find_slots_erro_inesperado"
    # Mario foi alertado (via _handoff_sem_calendar).
    para_mario = [
        c for c in jurichat.send_message.call_args_list if c.args[0] == "mario-conv"
    ]
    assert len(para_mario) >= 1


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
    # sem palavra de urgência: isola o handoff do escalonamento 1.12
    transcript = "Lead: preciso falar com um atendente humano"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="handoff",
            mensagem="Vou te conectar com a nossa equipe pra resolver isso, tá?",
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
    # G4 (2026-06-16): handoff NÃO é mais mudo — avisa o LEAD (com a mensagem
    # do Claude) E notifica o Mario. Dois envios.
    assert jurichat.send_message.await_count == 2
    destinos = [c.args[0] for c in jurichat.send_message.await_args_list]
    assert "C-1" in destinos          # lead recebeu o aviso
    assert "mario-conv" in destinos   # Mario foi notificado
    msg_lead = next(
        c.args[1] for c in jurichat.send_message.await_args_list
        if c.args[0] == "C-1"
    )
    assert "nossa equipe" in msg_lead
    # G4 fix (revisão): abre human-support ANTES do send (senão Jurichat 400).
    jurichat.start_human_support.assert_any_await("C-1")


@pytest.mark.asyncio
async def test_last_line_outbound_does_not_handoff_just_reschedules(db_conn):
    """Defensive policy: última msg OUTBOUND (atendente) NÃO causa
    AGUARDANDO_HUMANO porque não dá pra distinguir entre nossa própria
    resposta vs humano real respondendo. Só atualiza hash e reschedule.

    Trade-off documentado em scheduler.py: humano real que responde
    pelo Jurichat web não pausa o bot automaticamente (precisa fazer
    manualmente). Bug oposto (bot achar que era humano quando era ele
    mesmo) é pior — trava a conversa."""
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
    # Lead segue em em_conversa (não cai pra AGUARDANDO_HUMANO).
    assert lead["estado"] == Estado.EM_CONVERSA
    # Hash atualizado pra refletir o que processamos.
    assert lead["ultimo_transcript_hash"] == _sha(transcript)
    # proxima_acao_em foi rescheduled (não None — vai pollar de novo).
    assert lead["proxima_acao_em"] is not None
    # Claude NÃO foi chamado (nada novo do lead pra responder).
    triagem_fn.assert_not_called()
    # Nada foi enviado pra ninguém.
    jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_outbound_multiline_last_message_does_not_reinvoke_claude(
    db_conn, respx_mock,
):
    """Regressão: resposta multi-linha do PRÓPRIO BOT (ex.: bullets do
    oferecer_horarios terminando em "Qual prefere?") não pode furar o
    Signal 1. Usa o get_conversation REAL (via respx) porque o bug morava
    no builder do transcript: newline interno preservado fazia a última
    linha física não começar com "Atendente:", o Signal 1 falhava e o
    Claude era re-invocado sobre a própria mensagem do bot (custo de API
    + risco de resposta duplicada ao lead)."""
    respx_mock.get("https://api.jurichat.com/conversation/C-1").mock(
        return_value=httpx.Response(200, json={
            "data": {
                "id": "C-1",
                "person": {"name": "Maria"},
                "messages": [
                    {
                        "content": "quero agendar",
                        "direction": "INBOUND", "type": "text",
                    },
                    {
                        "content": (
                            "Tenho estes horários:\n• ter 14h00\n"
                            "• qua 10h00\nQual prefere?"
                        ),
                        "direction": "OUTBOUND", "type": "text",
                    },
                ],
            },
        })
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = JurichatClient("jk-test", "https://api.jurichat.com")
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))
    try:
        await run_poll_cycle(
            get_db=lambda: db_conn,
            jurichat=jurichat,
            triagem_fn=triagem_fn,
            mario_conversation_id="mario-conv",
            max_turnos=20,
        )
    finally:
        await jurichat.aclose()

    lead = get_lead_by_conversation(db_conn, "C-1")
    # Última mensagem é do bot → só atualiza hash e reschedule.
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["proxima_acao_em"] is not None
    assert lead["ultimo_transcript_hash"] == _sha(
        "Lead: quero agendar\n"
        "Atendente: Tenho estes horários: • ter 14h00 • qua 10h00 Qual prefere?"
    )
    triagem_fn.assert_not_called()


@pytest.mark.asyncio
async def test_max_turnos_reached_notifies_and_handoff(db_conn):
    # P0 (24/jun): o teto agora conta a coluna `turnos` (resetável na
    # reativação), não o histórico vitalício de linhas `Lead:`. turnos >=
    # max_turnos dispara o handoff.
    transcript = "Lead: ainda estou pensando\nLead: me ajuda a decidir?"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale", turnos=20)

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
    # G4 (2026-06-16): max_turnos agora AVISA o lead antes do handoff +
    # notifica o Mario. Dois envios (lead não fica mais no vácuo).
    assert jurichat.send_message.await_count == 2
    destinos = [c.args[0] for c in jurichat.send_message.await_args_list]
    assert "C-1" in destinos          # lead avisado
    assert "mario-conv" in destinos   # Mario notificado
    # G4 fix (revisão): abre human-support ANTES do send (senão Jurichat 400).
    jurichat.start_human_support.assert_any_await("C-1")


@pytest.mark.asyncio
async def test_historico_longo_mas_turnos_zerado_nao_e_capado(db_conn):
    """P0 (auditoria 24/jun): o teto contava `Lead:` do transcript VITALÍCIO,
    então um lead reativado (turnos=0) com histórico longo (>=20 msgs) era
    jogado pra humano SEM o bot ler a mensagem nova. Agora o teto conta a
    coluna `turnos` (zerada na reativação), então ele é atendido normalmente."""
    transcript = "\n".join(f"Lead: msg {i}" for i in range(25))
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale", turnos=0)

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(
        return_value=Decisao(acao="responder", mensagem="Claro, vamos lá!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    triagem_fn.assert_awaited_once()              # o bot LEU a mensagem nova
    assert lead["estado"] == Estado.EM_CONVERSA   # NÃO foi capado pra humano


@pytest.mark.asyncio
async def test_triagem_incrementa_turnos(db_conn):
    """Cada triagem bem-sucedida conta um turno (bump_turnos) — é o que
    alimenta o teto de forma resetável."""
    transcript = "Lead: oi, tudo bem?"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale", turnos=3)

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(
        return_value=Decisao(acao="responder", mensagem="Oi! Tudo ótimo.")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["turnos"] == 4   # 3 -> 4 após a triagem


@pytest.mark.asyncio
async def test_reativacao_zera_turnos(db_conn):
    """Lead que volta (encerrado/FU) com turnos altos é reativado com o teto
    zerado, pra não bater o teto na 1a mensagem nova (raiz do P0 24/jun)."""
    transcript = "Lead: oi, voltei! mudei de ideia, quero marcar mesmo"
    _insert_lead_estado(
        db_conn, Estado.ENCERRADO_SEM_RESPOSTA, hash_="stale", turnos=19,
    )

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(
        return_value=Decisao(acao="responder", mensagem="Que bom que voltou!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA   # reativado
    # Zerado na reativação (0); se o mesmo ciclo já reprocessou no polling, a
    # triagem bumpa pra 1 — qualquer um dos dois confirma o reset (≠ 19).
    assert lead["turnos"] in (0, 1)


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
    """Contrato novo (auditoria 2026-06-11): em_conversa ocioso ha mais
    de fu1_apos_horas (COALESCE(ultima_msg, criado_em)) vence pro
    follow-up MESMO com proxima_acao_em reagendada pelo poll (fix
    starvation). E lead recem-criado sem msg NAO vence."""
    _insert_lead_due_for_poll(db_conn, ultima_msg_lead_em=None)
    db_conn.execute(
        "UPDATE leads SET criado_em = datetime('now', '-50 hours') "
        "WHERE jurichat_conversation_id = 'C-1'"
    )

    from noviello_funil.state import list_leads_vencidos
    assert len(list_leads_vencidos(db_conn, fu1_apos_horas=48)) == 1
    db_conn.execute(
        "UPDATE leads SET criado_em = datetime('now') "
        "WHERE jurichat_conversation_id = 'C-1'"
    )
    assert len(list_leads_vencidos(db_conn, fu1_apos_horas=48)) == 0


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


# --- Calendar actions ---------------------------------------------------

def _make_calendar(slots: list[Slot] | None = None, *, with_meet: bool = True):
    """Fake calendar client com find_available_slots + create_event."""
    fake = MagicMock()
    fake.find_available_slots = AsyncMock(return_value=slots or [])
    event_response: dict = {"id": "evt-1"}
    if with_meet:
        event_response["hangoutLink"] = "https://meet.google.com/xyz-test"
    fake.create_event = AsyncMock(return_value=event_response)
    return fake


def _calendar_config(client=None):
    return CalendarConfig(
        client=client,
        business_hours_start=14, business_hours_end=19,
        slot_min=30, buffer_min=0,
        lookahead_days=5, num_slots=3,
    )


@pytest.mark.asyncio
async def test_oferecer_horarios_substitui_placeholder_e_envia(db_conn):
    """Claude pediu agendamento → bot busca slots reais e substitui placeholder."""
    transcript = "Atendente: Oi\nLead: Quero agendar uma conversa"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    tz = ZoneInfo("America/Sao_Paulo")
    slots = [
        Slot(start=datetime.datetime(2026, 6, 9, 14, 0, tzinfo=tz), duration_min=30),
        Slot(start=datetime.datetime(2026, 6, 9, 14, 30, tzinfo=tz), duration_min=30),
        Slot(start=datetime.datetime(2026, 6, 10, 15, 0, tzinfo=tz), duration_min=30),
    ]

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="oferecer_horarios",
            mensagem="Claro! Tenho esses horários:\n\n{{HORARIOS}}\n\nQual prefere?",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=_make_calendar(slots)),
    )

    sent_text = jurichat.send_message.call_args[0][1]
    assert "{{HORARIOS}}" not in sent_text  # placeholder substituído
    assert "ter (09/jun) às 14h" in sent_text
    assert "ter (09/jun) às 14h30" in sent_text
    assert "qua (10/jun) às 15h" in sent_text
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # ainda em conversa


@pytest.mark.asyncio
async def test_reoferta_exclui_ja_oferecidos_e_acumula(db_conn):
    """G1 (2026-06-16): ao re-oferecer, o bot exclui os horários já
    oferecidos (passa exclude_isos) e ACUMULA no horarios_oferecidos —
    o lead vê horários NOVOS e o Signal 1.8 ainda casa qualquer ofertado.
    Datas FUTURAS pra o filtro S5 (expirados) não limpar a oferta anterior."""
    transcript = (
        "Atendente: Tenho esses horários\nLead: não estou disponível nesses"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    lead_id = get_lead_by_conversation(db_conn, "C-1")["id"]

    ja = [
        {"iso": "2026-06-23T14:00:00-03:00", "label": "ter (23/jun) às 14h"},
        {"iso": "2026-06-23T18:30:00-03:00", "label": "ter (23/jun) às 18h30"},
    ]
    set_horarios_oferecidos(db_conn, lead_id, ja)

    tz = ZoneInfo("America/Sao_Paulo")
    novos = [Slot(start=datetime.datetime(2026, 6, 24, 10, 0, tzinfo=tz),
                  duration_min=30)]
    fake_cal = _make_calendar(novos)
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="oferecer_horarios",
            mensagem="Sem problema! Tenho também:\n\n{{HORARIOS}}\n\nQual prefere?",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
        calendar=_calendar_config(client=fake_cal),
    )

    # 1. find_available_slots recebeu os ISOs já oferecidos pra EXCLUIR.
    kwargs = fake_cal.find_available_slots.call_args.kwargs
    assert kwargs["exclude_isos"] == {
        "2026-06-23T14:00:00-03:00", "2026-06-23T18:30:00-03:00",
    }
    # 2. ACUMULOU: os 2 antigos + o novo (re-oferta não apaga o histórico).
    acumulado = {o["iso"] for o in get_horarios_oferecidos(db_conn, lead_id)}
    assert acumulado == {
        "2026-06-23T14:00:00-03:00", "2026-06-23T18:30:00-03:00",
        "2026-06-24T10:00:00-03:00",
    }


@pytest.mark.asyncio
async def test_oferecer_horarios_sem_calendar_vira_handoff(db_conn):
    """Sem Google configurado → degradar pra handoff cortês."""
    transcript = "Atendente: Oi\nLead: Quero agendar"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="oferecer_horarios",
            mensagem="Tenho esses horários: {{HORARIOS}}",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=None,  # explicitamente sem calendar
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    sent_text = jurichat.send_message.call_args_list[0][0][1]
    assert "Mario" in sent_text or "advogado" in sent_text.lower()


@pytest.mark.asyncio
async def test_confirmar_horario_cria_evento_com_email_e_meet(db_conn):
    """Lead escolheu horário + email na transcrição → evento com Meet link."""
    transcript = (
        "Lead: Quero agendar\n"
        "Atendente: Qual seu email?\n"
        "Lead: jose@exemplo.com\n"
        "Atendente: Tenho ter 14h, ter 14h30, qua 15h\n"
        "Lead: A terça 14h tá bom"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem=(
                "Perfeito! Agendado pra {{HORARIO_CONFIRMADO}}. "
                "Link Meet: {{MEET_LINK}}"
            ),
            horario_escolhido_iso="2027-06-08T14:00:00-03:00",
            lead_email="jose@exemplo.com",
            resumo_caso="Inventário, pai faleceu 20 dias, 3 herdeiros, SP",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Evento criado com email
    calendar_client.create_event.assert_awaited_once()
    call_kwargs = calendar_client.create_event.call_args.kwargs
    assert call_kwargs["lead_email"] == "jose@exemplo.com"
    assert call_kwargs["lead_nome"] == "Maria"

    # NOVO COMPORTAMENTO: lead PERMANECE em_conversa após confirmar
    # (pra poder processar resposta a lembretes — ex: "preciso remarcar").
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    # Reunião salva no DB com event_id + meet_link pro reminder cycle.
    assert lead["reuniao_em"] == "2027-06-08T14:00:00-03:00"
    assert lead["reuniao_meet_link"] == "https://meet.google.com/xyz-test"
    assert lead["reuniao_event_id"] == "evt-1"

    # Mensagem pro lead substituiu AMBOS placeholders
    sent_text = jurichat.send_message.call_args_list[0][0][1]
    assert "{{HORARIO_CONFIRMADO}}" not in sent_text
    assert "{{MEET_LINK}}" not in sent_text
    assert "ter (08/jun) às 14h" in sent_text
    assert "https://meet.google.com/xyz-test" in sent_text


@pytest.mark.asyncio
async def test_confirmar_horario_grava_reuniao_antes_de_enviar(db_conn, monkeypatch):
    """D1 (auditoria 24/jun): set_reuniao roda ANTES do send_message.

    Se o processo morrer entre o create_event e o envio da confirmação
    (deploy/OOM/restart do scheduler), a reunião já está persistida → os
    lembretes 24h/2h/30min saem e o evento não fica órfão no Google. Antes
    o set_reuniao vinha DEPOIS do send, abrindo a janela do no-show silencioso.
    """
    transcript = (
        "Lead: Quero agendar\n"
        "Atendente: Qual seu email?\n"
        "Lead: jose@exemplo.com\n"
        "Atendente: Tenho ter 14h\n"
        "Lead: A terça 14h tá bom"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado pra {{HORARIO_CONFIRMADO}}.",
            horario_escolhido_iso="2027-06-08T14:00:00-03:00",
            lead_email="jose@exemplo.com",
            resumo_caso="Inventário SP",
        )
    )

    # Espiona a ORDEM relativa entre set_reuniao (persistência no DB) e o
    # send_message (envio ao lead). O primeiro "send" do log é a confirmação;
    # o notify_mario manda outro depois, mas só o índice do primeiro importa.
    ordem: list[str] = []
    import noviello_funil.scheduler as sch

    real_set_reuniao = sch.set_reuniao

    def _spy_set_reuniao(*args, **kwargs):
        ordem.append("set_reuniao")
        return real_set_reuniao(*args, **kwargs)

    monkeypatch.setattr(sch, "set_reuniao", _spy_set_reuniao)

    async def _spy_send(*args, **kwargs):
        ordem.append("send")
        return {"id": "msg-1"}

    jurichat.send_message = AsyncMock(side_effect=_spy_send)

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    assert "set_reuniao" in ordem, "set_reuniao deveria ter sido chamado"
    assert "send" in ordem, "send_message deveria ter sido chamado"
    assert ordem.index("set_reuniao") < ordem.index("send"), (
        f"set_reuniao deve preceder o envio ao lead (ordem={ordem})"
    )
    # Regressão: a reunião ficou de fato persistida.
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] == "2027-06-08T14:00:00-03:00"
    assert lead["reuniao_event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_confirmar_horario_double_booking_reoferece(db_conn):
    """D2 (auditoria 24/jun): OUTRO lead já tem reunião exatamente neste
    horário → o bot NÃO cria evento duplicado; reoferece a agenda atualizada.

    O find_available_slots só lê o freeBusy do Calendar (eventual-consistente),
    então o DB é a fonte de verdade pra barrar double-booking no confirmar.
    """
    slot_iso = "2027-06-08T14:00:00-03:00"
    # Lead A (outro, NÃO due → o poll não o processa) já marcou esse horário.
    db_conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, reuniao_em, reuniao_event_id)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("L-A", "C-A", "5511000000000", "Outro Lead",
         Estado.EM_CONVERSA, slot_iso, "evt-A"),
    )
    # Lead B (o due) tenta confirmar O MESMO horário.
    transcript = (
        "Lead: jose@exemplo.com\n"
        "Atendente: Tenho ter 14h\n"
        "Lead: A terça 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado pra {{HORARIO_CONFIRMADO}}.",
            horario_escolhido_iso=slot_iso,
            lead_email="jose@exemplo.com",
            resumo_caso="Inventário SP",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # NÃO criou evento (slot já ocupado por outro lead).
    calendar_client.create_event.assert_not_awaited()
    lead_b = get_lead_by_conversation(db_conn, "C-1")
    assert lead_b["reuniao_em"] is None              # B não marcou
    assert lead_b["estado"] == Estado.EM_CONVERSA    # segue na conversa
    # Reofereceu (mensagem menciona horário/agenda) e limpou os slots vencidos.
    sent_text = jurichat.send_message.call_args_list[0][0][1]
    assert "horário" in sent_text.lower() or "agenda" in sent_text.lower()
    # Lead A permanece intacto.
    lead_a = get_lead_by_conversation(db_conn, "C-A")
    assert lead_a["reuniao_em"] == slot_iso


@pytest.mark.asyncio
async def test_propor_com_calendar_e_email_redireciona_pra_oferecer_horarios(db_conn):
    """Bug em campo (2026-06-09): Claude usou 'propor' pra lead pronto
    pra fechar, virou 'vou encaminhar pro Dr. Mario'. Guardrail força
    agendamento direto se calendar está disponível + email já existe."""
    transcript = (
        "Atendente: Como funciona?\n"
        "Lead: Meu email é joao@exemplo.com. Quanto custa?"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    tz = ZoneInfo("America/Sao_Paulo")
    slots = [
        Slot(start=datetime.datetime(2026, 6, 9, 14, 0, tzinfo=tz), duration_min=30),
        Slot(start=datetime.datetime(2026, 6, 9, 14, 30, tzinfo=tz), duration_min=30),
        Slot(start=datetime.datetime(2026, 6, 10, 15, 0, tzinfo=tz), duration_min=30),
    ]

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar(slots)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="propor",
            mensagem="Vou encaminhar pro Dr. Mario Noviello.",
            resumo_caso="Inventário SP",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Não foi pra aguardando_humano — agendamento foi disparado.
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    # Mensagem ofereceu horários (não a do Claude com "Dr. Mario")
    sent_text = jurichat.send_message.call_args_list[0][0][1]
    assert "Dr. Mario" not in sent_text
    assert "Mario Noviello" not in sent_text
    assert "ter (09/jun) às 14h" in sent_text
    assert "nossa equipe" in sent_text.lower() or "videochamada" in sent_text.lower()


@pytest.mark.asyncio
async def test_propor_com_calendar_sem_email_pede_email(db_conn):
    """Lead pronto pra fechar SEM email na transcrição → bot pede email
    primeiro (em vez de handoff humano)."""
    transcript = "Lead: Quanto custa pra fazer o inventário?"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="propor",
            mensagem="Vou encaminhar pro Mario.",
            resumo_caso="x",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # não foi aguardando_humano
    calendar_client.create_event.assert_not_awaited()
    sent_text = jurichat.send_message.call_args_list[0][0][1]
    assert "email" in sent_text.lower()
    assert "videochamada" in sent_text.lower() or "meet" in sent_text.lower()
    assert "Mario" not in sent_text  # nada de "passar pro Mario"


@pytest.mark.asyncio
async def test_remarcar_reuniao_cancela_evento_e_oferece_novos(db_conn):
    """Lead pediu remarcação → bot cancela evento antigo, limpa DB,
    oferece novos horários."""
    transcript = "Lead: Preciso remarcar, não vou poder amanhã"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    # Lead já tinha reunião marcada — simula estado pós-confirmar.
    db_conn.execute(
        """UPDATE leads SET
           reuniao_em='2026-06-10T17:00:00-03:00',
           reuniao_event_id='evt-antigo',
           reuniao_meet_link='https://meet.google.com/antigo',
           lembrete_24h_enviado_em=datetime('now')
           WHERE jurichat_conversation_id='C-1'"""
    )

    tz = ZoneInfo("America/Sao_Paulo")
    novos_slots = [
        Slot(start=datetime.datetime(2026, 6, 11, 14, 0, tzinfo=tz), duration_min=30),
        Slot(start=datetime.datetime(2026, 6, 11, 14, 30, tzinfo=tz), duration_min=30),
        Slot(start=datetime.datetime(2026, 6, 12, 15, 0, tzinfo=tz), duration_min=30),
    ]

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar(novos_slots)
    calendar_client.cancel_event = AsyncMock(return_value=None)

    triagem_fn = await _triagem_returning(
        Decisao(
            acao="remarcar_reuniao",
            mensagem="Sem problemas! Vou liberar o horário. Outros disponíveis:\n\n{{HORARIOS}}\n\nQual prefere?",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Evento antigo foi cancelado
    calendar_client.cancel_event.assert_awaited_once_with("evt-antigo")
    # DB limpo: reuniao_em volta pra None
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] is None
    assert lead["reuniao_event_id"] is None
    assert lead["lembrete_24h_enviado_em"] is None
    # Lead permanece em_conversa pra próximo turno escolher novo horário
    assert lead["estado"] == Estado.EM_CONVERSA
    # send_message é chamado 2x: notify_mario PRIMEIRO, depois lead.
    # Mensagem do lead (a última) substituiu placeholder com novos horários.
    sent_text = jurichat.send_message.call_args_list[-1][0][1]
    assert "{{HORARIOS}}" not in sent_text
    assert "qui (11/jun) às 14h" in sent_text


@pytest.mark.asyncio
async def test_cancelar_reuniao_avisa_mario_e_nao_oferece_horarios(db_conn):
    """Lead DESMARCOU sem remarcar (pedido Mario 2026-06-10): bot cancela
    evento, NÃO oferece novos horários, e avisa o Mario na hora."""
    transcript = "Lead: Pode desmarcar a reunião, alguns não vão participar"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    db_conn.execute(
        """UPDATE leads SET
           reuniao_em='2026-06-10T17:00:00-03:00',
           reuniao_event_id='evt-cancelar',
           reuniao_meet_link='https://meet.google.com/x',
           lembrete_24h_enviado_em=datetime('now')
           WHERE jurichat_conversation_id='C-1'"""
    )

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    calendar_client.cancel_event = AsyncMock(return_value=None)

    triagem_fn = await _triagem_returning(
        Decisao(
            acao="cancelar_reuniao",
            mensagem="Entendido! Vou desmarcar. Quando quiser retomar, é só chamar.",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Evento cancelado no Calendar
    calendar_client.cancel_event.assert_awaited_once_with("evt-cancelar")
    # DB limpo
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] is None
    assert lead["reuniao_event_id"] is None
    assert lead["lembrete_24h_enviado_em"] is None
    assert lead["estado"] == Estado.EM_CONVERSA
    # NÃO ofereceu novos horários
    calendar_client.find_available_slots.assert_not_awaited()
    # 2 sends: confirmação ao lead + AVISO ao Mario
    enviados = jurichat.send_message.call_args_list
    destinos = [c[0][0] for c in enviados]
    assert "C-1" in destinos        # confirmação ao lead
    assert "mario-conv" in destinos  # aviso ao Mario
    aviso = next(c[0][1] for c in enviados if c[0][0] == "mario-conv")
    assert "DESMARCADA" in aviso
    assert lead["contato_nome"] in aviso


@pytest.mark.asyncio
async def test_confirmar_horario_sem_email_guardrail_pede_email(db_conn):
    """Bug real (2026-06-08): Claude pulou turno 1 e foi direto pra
    confirmar SEM email. Guardrail bloqueia e pede email manualmente."""
    transcript = "Lead: 14h"  # sem email na transcrição
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado. O Mario vai te ligar.",  # texto problemático
            horario_escolhido_iso="2027-06-08T14:00:00-03:00",
            lead_email=None,  # ← guardrail trigger
            resumo_caso="caso x",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Evento NÃO criado (guardrail bloqueou)
    calendar_client.create_event.assert_not_awaited()
    # Lead continua em_conversa (não foi pra aguardando_humano)
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    # Bot pediu email manualmente
    sent_text = jurichat.send_message.call_args_list[0][0][1]
    assert "email" in sent_text.lower()
    assert "videochamada" in sent_text.lower() or "meet" in sent_text.lower()
    # NÃO mandou texto do Claude com "vai te ligar"
    assert "vai te ligar" not in sent_text.lower()




@pytest.mark.asyncio
async def test_confirmar_horario_sem_iso_registra_erro(db_conn):
    """Claude esqueceu horario_escolhido_iso → erro, não cria evento."""
    transcript = "Lead: A terça tá bom"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado",
            horario_escolhido_iso=None,  # ESQUECEU
            resumo_caso="caso x",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    calendar_client.create_event.assert_not_awaited()
    jurichat.send_message.assert_not_awaited()
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["erro_atual"] == "claude_horario_iso_ausente"
    assert lead["estado"] == Estado.EM_CONVERSA  # não progrediu


# --- sync: canal de alertas do Mario --------------------------------------

@pytest.mark.asyncio
async def test_sync_ignora_conversa_de_alertas_do_mario(db_conn):
    """A conversa do MARIO_CONVERSATION_ID nunca vira lead — senão o bot
    responderia as próprias notificações que envia pro Mario."""
    # DB precisa ter >=1 lead pra não cair no baseline da primeira execução.
    _insert_lead_due_for_poll(db_conn, jurichat_lead_id="L-0",
                              conversation_id="C-0")

    fake = MagicMock()
    fake.list_active_conversations = AsyncMock(return_value=[
        {
            "id": "C-MARIO-ALERTAS",
            "person": {"id": "P-MARIO", "phoneNumber": "5511000000000",
                       "name": "Mario"},
            "isArchived": False, "isGroup": False,
            "responsables": [],
        },
    ])
    fake.get_lead_tags = AsyncMock(return_value=[])

    stats = await sync_jurichat_conversations(
        get_db=lambda: db_conn,
        jurichat=fake,
        inbox_id="inbox-1",
        mario_conversation_id="C-MARIO-ALERTAS",
    )

    # Conversa do Mario NÃO virou lead.
    assert get_lead_by_conversation(db_conn, "C-MARIO-ALERTAS") is None
    assert stats["novos"] == 0


# --- Signal 0: humano assumiu a conversa -----------------------------------

@pytest.mark.asyncio
async def test_humano_assumiu_conversa_pausa_bot_sem_chamar_claude(db_conn):
    """user da conversa != BOT IA → pausa imediata, Claude nem é chamado."""
    transcript = "Lead: e aí, novidades?"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = MagicMock()
    jurichat.get_conversation = AsyncMock(return_value={
        "transcription": transcript,
        "user": {"id": "USR-HUMANO-MARIO", "name": "Mario Noviello"},
    })
    jurichat.send_message = AsyncMock()
    jurichat.start_human_support = AsyncMock()
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        bot_user_id="USR-BOT-IA",
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    triagem_fn.assert_not_called()
    jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_conversa_atribuida_ao_bot_segue_normal(db_conn):
    """user da conversa == BOT IA → fluxo normal (Claude responde)."""
    transcript = "Lead: quero saber mais"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = MagicMock()
    jurichat.get_conversation = AsyncMock(return_value={
        "transcription": transcript,
        "user": {"id": "USR-BOT-IA", "name": "BOT IA"},
    })
    jurichat.send_message = AsyncMock(return_value={"id": "m"})
    jurichat.start_human_support = AsyncMock(return_value={"success": True})
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Claro!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        bot_user_id="USR-BOT-IA",
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA
    jurichat.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_sem_bot_user_id_ignora_user_da_conversa(db_conn):
    """Backwards-compat: sem JURICHAT_BOT_USER_ID, user humano na
    conversa NÃO pausa (comportamento legado pré-feature)."""
    transcript = "Lead: oi"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = MagicMock()
    jurichat.get_conversation = AsyncMock(return_value={
        "transcription": transcript,
        "user": {"id": "USR-QUALQUER", "name": "THS - Midia"},
    })
    jurichat.send_message = AsyncMock(return_value={"id": "m"})
    jurichat.start_human_support = AsyncMock(return_value={"success": True})
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Olá!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        # bot_user_id NÃO passado (default "")
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA  # não pausou
    jurichat.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_poll_neutraliza_lead_da_conversa_de_alertas(db_conn):
    """Lead pré-existente da conversa de alertas (criado antes do
    guardrail do sync) é pausado no poll sem chamar a API."""
    _insert_lead_due_for_poll(
        db_conn, conversation_id="C-ALERTAS-MARIO", transcript_hash="stale",
    )

    jurichat = MagicMock()
    jurichat.get_conversation = AsyncMock(
        side_effect=AssertionError("must not fetch alert channel"),
    )
    triagem_fn = AsyncMock(side_effect=AssertionError("must not call Claude"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="C-ALERTAS-MARIO",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-ALERTAS-MARIO")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["proxima_acao_em"] is None
    jurichat.get_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_lead_novo_notifica_mario(db_conn):
    """Lead novo elegível → alerta '🆕 Lead novo' no canal do Mario."""
    # DB com >=1 lead pra não cair no baseline.
    _insert_lead_due_for_poll(db_conn, jurichat_lead_id="L-0",
                              conversation_id="C-0")

    fake = MagicMock()
    fake.list_active_conversations = AsyncMock(return_value=[
        {
            "id": "C-NOVO",
            "person": {"id": "P-NOVO", "phoneNumber": "5500000000002",
                       "name": "Fulano Teste"},
            "isArchived": False, "isGroup": False,
            "responsables": [],
        },
    ])
    fake.get_lead_tags = AsyncMock(return_value=[])
    fake.start_human_support = AsyncMock(return_value={"success": True})
    fake.send_message = AsyncMock(return_value={"id": "m"})

    stats = await sync_jurichat_conversations(
        get_db=lambda: db_conn,
        jurichat=fake,
        inbox_id="inbox-1",
        mario_conversation_id="C-ALERTAS",
    )

    assert stats["novos"] == 1
    # Notificação enviada pro canal de alertas com os dados do lead
    fake.send_message.assert_awaited_once()
    conv_dest, texto = fake.send_message.call_args[0]
    assert conv_dest == "C-ALERTAS"
    assert "Lead novo" in texto
    assert "Fulano Teste" in texto
    assert "5500000000002" in texto


@pytest.mark.asyncio
async def test_sync_lead_com_responsavel_notifica_sem_atender(db_conn):
    """Lead novo COM responsável → bot não atende, MAS Mario é avisado
    (pedido 2026-06-12: todo lead novo gera alerta no WhatsApp)."""
    _insert_lead_due_for_poll(db_conn, jurichat_lead_id="L-0",
                              conversation_id="C-0")

    fake = MagicMock()
    fake.list_active_conversations = AsyncMock(return_value=[
        {
            "id": "C-COM-DONO",
            "person": {"id": "P-X", "phoneNumber": "5511777776666",
                       "name": "Cliente Antigo"},
            "isArchived": False, "isGroup": False,
            "responsables": [{"id": "USR-HUMANO"}],
        },
    ])
    fake.get_lead_tags = AsyncMock(return_value=[])
    fake.start_human_support = AsyncMock(return_value={"success": True})
    fake.send_message = AsyncMock(return_value={"id": "m"})

    stats = await sync_jurichat_conversations(
        get_db=lambda: db_conn,
        jurichat=fake,
        inbox_id="inbox-1",
        mario_conversation_id="C-ALERTAS",
    )

    assert stats["ignoradas"] == 1
    fake.send_message.assert_awaited_once()
    conv_dest, texto = fake.send_message.call_args[0]
    assert conv_dest == "C-ALERTAS"
    assert "Lead novo" in texto
    assert "Cliente Antigo" in texto
    assert "NÃO vai atender" in texto


@pytest.mark.asyncio
async def test_sync_lead_com_tag_exclusao_notifica_sem_atender(db_conn):
    """Lead novo com etiqueta de exclusão → bot não atende, Mario sabe."""
    _insert_lead_due_for_poll(db_conn, jurichat_lead_id="L-0",
                              conversation_id="C-0")

    fake = MagicMock()
    fake.list_active_conversations = AsyncMock(return_value=[
        {
            "id": "C-TAGUEADO",
            "person": {"id": "P-T", "phoneNumber": "5511666665555",
                       "name": "Adverso Teste"},
            "isArchived": False, "isGroup": False,
            "responsables": [],
        },
    ])
    fake.get_lead_tags = AsyncMock(return_value=["Advogado adverso"])
    fake.start_human_support = AsyncMock(return_value={"success": True})
    fake.send_message = AsyncMock(return_value={"id": "m"})

    stats = await sync_jurichat_conversations(
        get_db=lambda: db_conn,
        jurichat=fake,
        inbox_id="inbox-1",
        mario_conversation_id="C-ALERTAS",
    )

    assert stats["ignoradas"] == 1
    fake.send_message.assert_awaited_once()
    _, texto = fake.send_message.call_args[0]
    assert "Advogado adverso" in texto
    assert "NÃO vai atender" in texto


@pytest.mark.asyncio
async def test_sync_baseline_primeira_execucao_nao_notifica(db_conn):
    """Primeira execução (DB vazio) registra baseline em silêncio —
    alertar 230 conversas históricas de uma vez seria spam."""
    fake = MagicMock()
    fake.list_active_conversations = AsyncMock(return_value=[
        {
            "id": f"C-HIST-{i}",
            "person": {"id": f"P-{i}", "phoneNumber": f"55110000000{i}",
                       "name": f"Histórico {i}"},
            "isArchived": False, "isGroup": False,
            "responsables": [],
        }
        for i in range(3)
    ])
    fake.get_lead_tags = AsyncMock(return_value=[])
    fake.start_human_support = AsyncMock(return_value={"success": True})
    fake.send_message = AsyncMock()

    stats = await sync_jurichat_conversations(
        get_db=lambda: db_conn,
        jurichat=fake,
        inbox_id="inbox-1",
        mario_conversation_id="C-ALERTAS",
    )

    assert stats["baseline"] == 3
    fake.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_guard_csv_protege_todos_os_canais_de_alerta(db_conn):
    """MARIO_CONVERSATION_ID com CSV: NENHUM dos canais vira lead."""
    _insert_lead_due_for_poll(db_conn, jurichat_lead_id="L-0",
                              conversation_id="C-0")

    fake = MagicMock()
    fake.list_active_conversations = AsyncMock(return_value=[
        {
            "id": "C-EQUIPE",
            "person": {"id": "P-EQ", "phoneNumber": "5511999990000",
                       "name": "Equipe"},
            "isArchived": False, "isGroup": False,
            "responsables": [],
        },
    ])
    fake.get_lead_tags = AsyncMock(return_value=[])
    fake.start_human_support = AsyncMock(return_value={"success": True})
    fake.send_message = AsyncMock()

    stats = await sync_jurichat_conversations(
        get_db=lambda: db_conn,
        jurichat=fake,
        inbox_id="inbox-1",
        mario_conversation_id="C-ALERTAS,C-EQUIPE",
    )

    assert get_lead_by_conversation(db_conn, "C-EQUIPE") is None
    assert stats["novos"] == 0
    fake.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmar_horario_faz_intake_juridiq(db_conn):
    """Lead agendou → intake cria Pessoa no Juridiq com a qualificação."""
    transcript = (
        "Lead: jose@exemplo.com\n"
        "Atendente: Tenho ter 14h\n"
        "Lead: fechado 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    juridiq = MagicMock()
    juridiq.search_person_by_phone = AsyncMock(return_value=None)
    juridiq.create_person = AsyncMock(return_value={"id": "P-NOVO"})

    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado pra {{HORARIO_CONFIRMADO}}! {{MEET_LINK}}",
            horario_escolhido_iso="2027-06-08T14:00:00-03:00",
            lead_email="jose@exemplo.com",
            resumo_caso="Inventário SP, 3 herdeiros",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
        juridiq=juridiq,
    )

    # Intake rodou: buscou por telefone e criou a pessoa
    juridiq.search_person_by_phone.assert_awaited_once_with("5511999999999")
    juridiq.create_person.assert_awaited_once()
    kwargs = juridiq.create_person.call_args.kwargs
    assert kwargs["name"] == "Maria"
    assert kwargs["email"] == "jose@exemplo.com"
    assert "Inventário SP" in kwargs["annotation"]
    # Notificação pro Mario menciona a ficha criada
    notify_text = jurichat.send_message.call_args_list[-1][0][1]
    assert "Juridiq" in notify_text


@pytest.mark.asyncio
async def test_confirmar_horario_sem_juridiq_segue_normal(db_conn):
    """Sem JURIDIQ_API_KEY (juridiq=None) → agendamento normal, sem intake."""
    transcript = "Lead: maria@x.com\nLead: 14h"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado {{HORARIO_CONFIRMADO}} {{MEET_LINK}}",
            horario_escolhido_iso="2027-06-08T14:00:00-03:00",
            lead_email="maria@x.com",
            resumo_caso="caso y",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
        juridiq=None,
    )

    calendar_client.create_event.assert_awaited_once()
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] is not None  # agendamento intacto


# --- Auditoria 2026-06-10: timezone/agendamento (Grupo B) -----------------

@pytest.mark.asyncio
async def test_confirmar_horario_iso_naive_vira_horario_de_brasilia(db_conn):
    """HIGH da auditoria: ISO sem offset era interpretado como UTC do VPS
    → evento 3h errado. Naive agora = America/Sao_Paulo."""
    transcript = "Lead: jose@x.com\nLead: 15h tá ótimo"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado {{HORARIO_CONFIRMADO}} {{MEET_LINK}}",
            horario_escolhido_iso="2027-06-15T15:00:00",  # SEM offset!
            lead_email="jose@x.com",
            resumo_caso="caso",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Evento criado com datetime AWARE em America/Sao_Paulo (15h BRT,
    # não 15h UTC = 12h BRT)
    call = calendar_client.create_event.call_args.kwargs
    start = call["start"]
    assert start.tzinfo is not None
    assert start.utcoffset() == datetime.timedelta(hours=-3)
    assert start.hour == 15
    # Mensagem formata 15h (hora local), e reuniao_em persiste COM offset
    sent = jurichat.send_message.call_args_list[0][0][1]
    assert "15h" in sent
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert "-03:00" in lead["reuniao_em"]


@pytest.mark.asyncio
async def test_confirmar_horario_no_passado_pede_novo_horario(db_conn):
    """HIGH da auditoria: horário no passado era aceito (evento morto,
    lead achando que agendou). Agora pede pro lead re-escolher."""
    transcript = "Lead: maria@x.com\nLead: pode ser 14h"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado!",
            horario_escolhido_iso="2020-01-01T14:00:00-03:00",  # PASSADO
            lead_email="maria@x.com",
            resumo_caso="caso",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    calendar_client.create_event.assert_not_awaited()
    sent = jurichat.send_message.call_args_list[0][0][1]
    assert "já passou" in sent
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["erro_atual"] == "horario_no_passado"
    assert lead["reuniao_em"] is None


@pytest.mark.asyncio
async def test_confirmar_horario_nao_string_nao_crasha(db_conn):
    """MEDIUM da auditoria: ISO não-string (int do LLM) estourava
    TypeError e envenenava o tick."""
    transcript = "Lead: x@x.com\nLead: 14h"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="ok",
            horario_escolhido_iso=20260615,  # int!
            lead_email="x@x.com",
            resumo_caso="caso",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )  # MUST NOT raise

    calendar_client.create_event.assert_not_awaited()
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["erro_atual"] == "claude_horario_iso_invalido"


@pytest.mark.asyncio
async def test_confirmar_segunda_reuniao_cancela_evento_antigo(db_conn):
    """HIGH da auditoria: confirmar nova reunião com uma já marcada
    deixava o evento antigo órfão no Calendar (double-booking)."""
    transcript = "Lead: jose@x.com\nLead: muda pra quinta 14h então"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    db_conn.execute(
        """UPDATE leads SET
           reuniao_em='2027-06-15T15:00:00-03:00',
           reuniao_event_id='evt-ANTIGO',
           reuniao_meet_link='https://meet.google.com/velho'
           WHERE jurichat_conversation_id='C-1'"""
    )

    jurichat = _make_jurichat(transcript)
    calendar_client = _make_calendar()
    calendar_client.cancel_event = AsyncMock(return_value=None)
    triagem_fn = await _triagem_returning(
        Decisao(
            acao="confirmar_horario",
            mensagem="Agendado {{HORARIO_CONFIRMADO}} {{MEET_LINK}}",
            horario_escolhido_iso="2027-06-17T14:00:00-03:00",
            lead_email="jose@x.com",
            resumo_caso="caso",
        )
    )

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
        calendar=_calendar_config(client=calendar_client),
    )

    # Evento antigo cancelado ANTES do novo ser criado
    calendar_client.cancel_event.assert_awaited_once_with("evt-ANTIGO")
    calendar_client.create_event.assert_awaited_once()
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_event_id"] == "evt-1"  # o novo (mock retorna evt-1)


# --- Auditoria 2026-06-11: reativação de leads (Grupo A) ------------------

def _insert_lead_estado(conn, estado, *, conv="C-1", hash_=None, turnos=0):
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, ultimo_transcript_hash, turnos)
           VALUES ('L-1', ?, '5511999999999', 'Maria', ?, ?, ?)""",
        (conv, estado, hash_, turnos),
    )


@pytest.mark.asyncio
async def test_lead_em_fu1_que_responde_eh_reativado(db_conn):
    """HIGH da auditoria: resposta de lead em FU1 era invisível (polling
    só olhava em_conversa) e ele ainda levava FU2 + encerramento."""
    transcript = "Atendente: Oi! Conseguiu ver?\nLead: sim! quero continuar"
    _insert_lead_estado(db_conn, Estado.FOLLOW_UP_1_ENVIADO, hash_="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Que bom!")
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
    assert lead["proxima_acao_em"] is not None  # de volta no polling


@pytest.mark.asyncio
async def test_lead_encerrado_que_volta_eh_reativado(db_conn):
    """HIGH da auditoria: lead que mandava mensagem após o encerramento
    silencioso ficava invisível pra sempre."""
    transcript = "Lead: oi, voltei! ainda dá pra fazer o inventário?"
    _insert_lead_estado(db_conn, Estado.ENCERRADO_SEM_RESPOSTA, hash_="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Claro!")
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


@pytest.mark.asyncio
async def test_fu_proprio_nao_reativa_lead(db_conn):
    """O hash muda quando NOSSO follow-up entra na transcrição — isso
    não pode reativar (loop infinito FU→reativa→FU)."""
    transcript = "Lead: oi\nAtendente: Oi! Conseguiu ver aquilo?"  # FU nosso
    _insert_lead_estado(db_conn, Estado.FOLLOW_UP_1_ENVIADO, hash_="stale")

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("não deve triagem"))

    await run_poll_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv",
        max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.FOLLOW_UP_1_ENVIADO  # não reativou
    # hash registrado pra não re-checar a mesma mudança todo tick
    assert lead["ultimo_transcript_hash"] is not None
    assert lead["ultimo_transcript_hash"] != "stale"


# --- Auditoria 2026-06-24: re-engaje de AGUARDANDO_HUMANO (P1) --------------

def _insert_transicao_ah(conn, lead_id, motivo):
    """Registra uma transição para AGUARDANDO_HUMANO com um motivo (define se o
    lead pode reabrir quando volta a falar)."""
    conn.execute(
        "INSERT INTO transicoes (lead_id, estado_novo, motivo) VALUES (?, ?, ?)",
        (lead_id, Estado.AGUARDANDO_HUMANO, motivo),
    )


@pytest.mark.asyncio
async def test_aguardando_humano_reabre_em_motivo_reabrivel(db_conn):
    """P1 (24/jun): lead em AGUARDANDO_HUMANO por motivo REABRÍVEL (ex:
    max_turnos) que volta a falar é reaberto pro bot + Mario re-alertado — em
    vez de virar vácuo (bot é o único atendimento)."""
    transcript = (
        "Atendente: vou te conectar com a equipe\n"
        "Lead: opa, mudei de ideia, quero marcar mesmo!"
    )
    _insert_lead_estado(
        db_conn, Estado.AGUARDANDO_HUMANO, hash_="stale", turnos=20,
    )
    lead = get_lead_by_conversation(db_conn, "C-1")
    _insert_transicao_ah(db_conn, lead["id"], "max_turnos")

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(
        return_value=Decisao(acao="responder", mensagem="Que bom que voltou!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.EM_CONVERSA   # reaberto
    assert lead["turnos"] in (0, 1)               # teto zerado (e talvez +1 bump)
    destinos = [c.args[0] for c in jurichat.send_message.await_args_list]
    assert "mario-conv" in destinos               # Mario re-alertado


@pytest.mark.asyncio
async def test_aguardando_humano_fica_mudo_em_motivo_terminal(db_conn):
    """Lead em AGUARDANDO_HUMANO por motivo TERMINAL (ex: opt_out) que volta a
    falar NÃO é reaberto — fica mudo de propósito (respeita o pedido de parar /
    o humano que assumiu)."""
    transcript = "Atendente: ok, não te mando mais\nLead: na verdade me manda sim"
    _insert_lead_estado(db_conn, Estado.AGUARDANDO_HUMANO, hash_="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    _insert_transicao_ah(db_conn, lead["id"], "opt_out")

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("terminal não reabre"))

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO   # NÃO reabriu
    assert lead["ultimo_transcript_hash"] == _sha(transcript)  # só registrou hash
    jurichat.send_message.assert_not_called()           # mudo


@pytest.mark.asyncio
async def test_aguardando_humano_ultima_linha_atendente_nao_reabre(db_conn):
    """Mesmo com motivo reabrível, se a última linha é Atendente: (humano
    respondeu pelo painel) o bot NÃO reabre por cima — só registra o hash."""
    transcript = "Lead: ainda dá pra agendar?\nAtendente: já te respondo!"
    _insert_lead_estado(db_conn, Estado.AGUARDANDO_HUMANO, hash_="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    _insert_transicao_ah(db_conn, lead["id"], "max_turnos")  # reabrível, mas...

    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("última linha atendente"))

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO   # não reabriu
    assert lead["ultimo_transcript_hash"] == _sha(transcript)


# --- Auditoria 2026-06-24: follow-up herda o Signal 0 (C2) -----------------

def _insert_lead_vencido_em_conversa(conn, *, conv="C-1"):
    """EM_CONVERSA sem atividade há 72h → vencido pro follow-up."""
    velho = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=72)
    ).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado, ultima_msg_lead_em)
           VALUES ('L-1', ?, '5511999999999', 'Maria', ?, ?)""",
        (conv, Estado.EM_CONVERSA, velho),
    )


def _jurichat_followup(conv_user_id=None):
    j = MagicMock()
    j.get_lead_tags = AsyncMock(return_value=[])
    payload = {"transcription": "Lead: oi"}
    if conv_user_id is not None:
        payload["user"] = {"id": conv_user_id, "name": "X"}
    j.get_conversation = AsyncMock(return_value=payload)
    j.start_human_support = AsyncMock()
    j.send_message = AsyncMock()
    return j


@pytest.mark.asyncio
async def test_followup_nao_dispara_quando_humano_assumiu(db_conn):
    """C2: humano assumiu a conversa pelo painel (conv.user != bot_user_id) → o
    follow-up pausa pra AGUARDANDO_HUMANO em vez de disparar FU por cima."""
    _insert_lead_vencido_em_conversa(db_conn)
    jurichat = _jurichat_followup(conv_user_id="humano-456")
    gerar = AsyncMock(side_effect=AssertionError("não deve gerar FU"))

    await run_followup_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, gerar_followup_msg=gerar,
        followup_2_apos_horas=72, encerramento_apos_horas=24,
        followup_1_apos_horas=48, bot_user_id="bot-123",
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    jurichat.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_followup_dispara_quando_bot_e_responsavel(db_conn):
    """Quando o responsável é o próprio bot, o follow-up dispara normalmente
    (o guard do C2 não bloqueia à toa)."""
    _insert_lead_vencido_em_conversa(db_conn)
    jurichat = _jurichat_followup(conv_user_id="bot-123")
    gerar = AsyncMock(return_value="Oi Maria, retomando nosso papo!")

    await run_followup_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, gerar_followup_msg=gerar,
        followup_2_apos_horas=72, encerramento_apos_horas=24,
        followup_1_apos_horas=48, bot_user_id="bot-123",
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.FOLLOW_UP_1_ENVIADO
    jurichat.send_message.assert_awaited()


# --- 1.12 escalonamento de urgência jurídica --------------------------------

@pytest.mark.asyncio
async def test_urgencia_escala_pro_mario_e_segue_atendendo(db_conn):
    transcript = "Lead: socorro, fui citado e a audiência é amanhã!"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Entendo a urgência, vamos te ajudar.")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    textos = [c[0][1] for c in jurichat.send_message.call_args_list]
    # alertou o Mario com urgência (🚨)
    assert any("URGÊNCIA" in t for t in textos)
    # E NÃO interrompeu: o bot ainda respondeu o lead normalmente
    assert any("vamos te ajudar" in t for t in textos)
    # marca persistida pra não repetir
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["urgencia_alertada_em"] is not None


@pytest.mark.asyncio
async def test_urgencia_nao_repete_se_ja_alertada(db_conn):
    transcript = "Lead: penhoraram minha conta de novo!"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    # lead já foi escalado antes
    db_conn.execute(
        "UPDATE leads SET urgencia_alertada_em = datetime('now') WHERE jurichat_conversation_id = ?",
        ("C-1",),
    )
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Ok, seguimos.")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    textos = [c[0][1] for c in jurichat.send_message.call_args_list]
    assert not any("URGÊNCIA" in t for t in textos)  # não re-alertou


@pytest.mark.asyncio
async def test_mensagem_comum_nao_escala_urgencia(db_conn):
    transcript = "Lead: oi, queria saber sobre inventário"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Claro, posso explicar!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    textos = [c[0][1] for c in jurichat.send_message.call_args_list]
    assert not any("URGÊNCIA" in t for t in textos)
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["urgencia_alertada_em"] is None


# --- 1.10 opt-out / LGPD ----------------------------------------------------

@pytest.mark.asyncio
async def test_opt_out_suprime_confirma_e_sai_do_funil(db_conn):
    from noviello_funil.opt_out import esta_suprimido

    transcript = "Lead: pode parar de me mandar mensagem, por favor"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    triagem_fn = AsyncMock(side_effect=AssertionError("não deve chamar Claude no opt-out"))

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    # registrou na supressão (pelo telefone do lead)
    assert esta_suprimido(db_conn, telefone="5511999999999")
    # confirmou de forma sóbria ao lead
    textos = [c[0][1] for c in jurichat.send_message.call_args_list]
    assert any("não vou mais te enviar" in t for t in textos)
    # saiu do funil automático
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    # não chamou o Claude (curto-circuitou antes da triagem)
    triagem_fn.assert_not_called()


# --- 1.6 reconhecer cliente existente ---------------------------------------

@pytest.mark.asyncio
async def test_cliente_existente_avisa_mario_e_segue(db_conn):
    from noviello_funil.person_index import chaves_telefone
    # popula o índice com o telefone do lead de teste (5511999999999)
    for ch in chaves_telefone("5511999999999"):
        db_conn.execute(
            "INSERT OR REPLACE INTO person_index "
            "(telefone_chave, person_id, nome, email) VALUES (?, ?, ?, ?)",
            (ch, "P-CLI", "Cliente Antigo", "cli@x.com"),
        )
    transcript = "Lead: oi, tudo bem? preciso de uma ajuda nova"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Claro, como posso ajudar?")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    textos = [c[0][1] for c in jurichat.send_message.call_args_list]
    # avisou o Mario que é cliente da casa
    assert any("JÁ É CLIENTE" in t and "Cliente Antigo" in t for t in textos)
    # e seguiu atendendo normalmente (não mudou o fluxo)
    assert any("como posso ajudar" in t for t in textos)
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["cliente_checado_em"] is not None


@pytest.mark.asyncio
async def test_lead_desconhecido_nao_aciona_aviso_de_cliente(db_conn):
    transcript = "Lead: oi, quero saber sobre usucapião"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")  # índice vazio
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Posso explicar!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    textos = [c[0][1] for c in jurichat.send_message.call_args_list]
    assert not any("JÁ É CLIENTE" in t for t in textos)
    # mesmo sem match, marca o check pra não repetir
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["cliente_checado_em"] is not None


# --- 1.7 conflito de interesse ----------------------------------------------

@pytest.mark.asyncio
async def test_conflito_de_interesse_alerta_so_no_canal_interno(db_conn):
    db_conn.execute(
        "INSERT INTO parte_contraria (nome_norm, processo, papel) VALUES (?,?,?)",
        ("joao reu souza", "1234567-89.2026.8.26.0100", "Requerido"),
    )
    transcript = "Lead: oi, preciso de um advogado"
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    # lead com nome que bate com a parte contrária
    db_conn.execute(
        "UPDATE leads SET contato_nome = ? WHERE jurichat_conversation_id = ?",
        ("João Réu Souza", "C-1"),
    )
    jurichat = _make_jurichat(transcript)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Claro, como ajudo?")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    # alerta de conflito existe e cita o processo
    conflito_calls = [
        c for c in jurichat.send_message.call_args_list if "CONFLITO" in c[0][1]
    ]
    assert conflito_calls, "deveria ter alertado conflito"
    assert "1234567-89.2026.8.26.0100" in conflito_calls[0][0][1]
    # CRÍTICO: a suspeita vai SÓ pro canal interno do Mario, nunca ao lead
    assert conflito_calls[0][0][0] == "mario-conv"
    # e o lead NÃO recebe nada sobre conflito (só a resposta normal)
    lead_msgs = [
        c[0][1] for c in jurichat.send_message.call_args_list if c[0][0] == "C-1"
    ]
    assert not any("CONFLITO" in t or "parte contrária" in t for t in lead_msgs)


# --- Signal 1.8: escolha de horário determinística (bugfix Camila 16/jun) ---

def _make_calendar_confirma():
    """CalendarConfig com create_event mockado (retorna Meet link)."""
    client = MagicMock()
    client.create_event = AsyncMock(return_value={
        "hangoutLink": "https://meet.google.com/xyz-fake", "id": "evt-1",
    })
    client.cancel_event = AsyncMock(return_value=None)
    return CalendarConfig(
        client=client, business_hours_start=14, business_hours_end=19,
        slot_min=30, buffer_min=0, lookahead_days=5, num_slots=4,
    )


@pytest.mark.asyncio
async def test_signal_1_8_escolha_deterministica_confirma_sem_claude(db_conn):
    """REGRESSÃO do bug Camila: o bot ofereceu horários, o lead escolheu um
    ('Ter (16/jun) às 14h') — o sistema confirma DIRETO, sem chamar o Claude
    (que derrapava pro intake e dropava a confirmação até o teto de turnos)."""
    from noviello_funil.state import set_horarios_oferecidos

    transcript = (
        "Atendente: Qual seu email?\nLead: camila@exemplo.com\n"
        "Atendente: Tenho esses horários:\n• ter (16/jun) às 14h\nQual prefere?\n"
        "Lead: Ter (16/jun) às 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2099-06-17T14:00:00-03:00", "label": "qua (17/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript)
    calendar = _make_calendar_confirma()
    # Claude NÃO pode ser chamado — a rede determinística confirma antes.
    triagem_fn = AsyncMock(side_effect=AssertionError("Claude não deve ser chamado"))

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] is not None                  # reunião criada
    assert lead["reuniao_meet_link"] == "https://meet.google.com/xyz-fake"
    assert lead["horarios_oferecidos"] is None             # escolha consumida
    calendar.client.create_event.assert_awaited_once()
    triagem_fn.assert_not_called()                         # Claude bypassado
    assert any(
        c.args[0] == "C-1" for c in jurichat.send_message.await_args_list
    )


@pytest.mark.asyncio
async def test_signal_1_8_resposta_ambigua_cai_no_claude(db_conn):
    """Se a escolha é ambígua ('14h' com vários slots às 14h), a rede NÃO casa
    e o turno segue normal pro Claude — sem confirmar errado."""
    from noviello_funil.state import set_horarios_oferecidos

    transcript = (
        "Atendente: Tenho esses horários:\n• ter (16/jun) às 14h\n"
        "• qua (17/jun) às 14h\nQual prefere?\nLead: 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
        {"iso": "2099-06-17T14:00:00-03:00", "label": "qua (17/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript)
    calendar = _make_calendar_confirma()
    # ambíguo → Claude é chamado (retorna responder pedindo o dia)
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="De qual dia, terça ou quarta?")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar,
    )

    calendar.client.create_event.assert_not_awaited()      # NÃO confirmou cego
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] is None
    assert lead["horarios_oferecidos"] is not None         # segue pendente


# --- Signal 1.8 endurecido (achados adversariais 16/jun) --------------------

@pytest.mark.asyncio
async def test_signal_1_8_usa_email_do_lead_nao_do_atendente(db_conn):
    """S1: email do atendente/assinatura ANTES do email do lead não pode ir
    pro convite. create_event recebe o email do LEAD (mais recente, linha
    Lead:), não o primeiro email do transcript inteiro."""
    from noviello_funil.state import set_horarios_oferecidos

    transcript = (
        "Atendente: pode escrever pra contato@noviello.adv.br se preferir\n"
        "Lead: camila@exemplo.com\n"
        "Atendente: Tenho ter (16/jun) às 14h\n"
        "Lead: Ter (16/jun) às 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript)
    calendar = _make_calendar_confirma()
    triagem_fn = AsyncMock(side_effect=AssertionError("Claude não deve ser chamado"))

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar,
    )

    calendar.client.create_event.assert_awaited_once()
    kwargs = calendar.client.create_event.call_args.kwargs
    assert kwargs["lead_email"] == "camila@exemplo.com"
    assert kwargs["lead_email"] != "contato@noviello.adv.br"


@pytest.mark.asyncio
async def test_signal_1_8_email_gap_pede_depois_confirma(db_conn):
    """S3: 1 slot oferecido, lead escolheu sem email → guardrail pede email;
    no tick seguinte o lead manda só o email → confirma o slot único
    deterministicamente (sem reabrir o bug Camila)."""
    from noviello_funil.state import set_horarios_oferecidos

    # Tick 1: lead escolheu o slot, sem email em parte alguma.
    transcript1 = (
        "Atendente: Tenho ter (16/jun) às 14h\n"
        "Lead: Ter (16/jun) às 14h"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript1)
    calendar = _make_calendar_confirma()
    triagem_fn = AsyncMock(side_effect=AssertionError("Claude não deve ser chamado"))

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar,
    )

    # Guardrail pediu email; NÃO criou evento; slot segue pendente.
    calendar.client.create_event.assert_not_awaited()
    sent = jurichat.send_message.call_args_list[0][0][1]
    assert "email" in sent.lower()
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["horarios_oferecidos"] is not None

    # Tick 2: lead responde SÓ com o email → confirma o slot único.
    # Reabre a janela de poll (tick 1 reagendou pro futuro).
    past = (
        datetime.datetime.utcnow() - datetime.timedelta(seconds=10)
    ).strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute(
        "UPDATE leads SET proxima_acao_em = ? WHERE jurichat_conversation_id = 'C-1'",
        (past,),
    )
    transcript2 = transcript1 + "\nLead: camila@exemplo.com"
    jurichat2 = _make_jurichat(transcript2)
    calendar2 = _make_calendar_confirma()

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat2, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar2,
    )

    calendar2.client.create_event.assert_awaited_once()
    assert calendar2.client.create_event.call_args.kwargs["lead_email"] == \
        "camila@exemplo.com"
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_em"] is not None
    assert lead["horarios_oferecidos"] is None


@pytest.mark.asyncio
async def test_signal_1_8_slots_no_passado_nao_cria_evento_limpa(db_conn):
    """S5: slots persistidos com ISO no PASSADO + resposta que casaria o
    label → não cria evento, limpa horarios_oferecidos, sem loop. Cai no
    Claude (que reabre a agenda)."""
    from noviello_funil.state import set_horarios_oferecidos

    transcript = (
        "Atendente: Tenho ter às 14h\n"
        "Lead: opa, a terça tá ótima"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2020-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript)
    calendar = _make_calendar_confirma()
    # Claude assume (slot morto) e responde normalmente.
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Vou te mostrar a agenda atualizada.")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar,
    )

    calendar.client.create_event.assert_not_awaited()
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["horarios_oferecidos"] is None   # limpo (sem loop)
    assert lead["reuniao_em"] is None


@pytest.mark.asyncio
async def test_signal_1_8_lead_com_reuniao_marcada_defere_ao_claude(db_conn):
    """S2: lead já tem reuniao_event_id + horarios_oferecidos pendentes +
    msg casando um slot → Signal 1.8 NÃO confirma (não cancela/recria). Defere
    ao Claude (que distingue remarcar de comentário casual)."""
    from noviello_funil.state import set_horarios_oferecidos

    transcript = (
        "Atendente: Tenho ter (16/jun) às 14h\n"
        "Lead: aquela terça 14h que você falou"
    )
    _insert_lead_due_for_poll(db_conn, transcript_hash="stale")
    db_conn.execute(
        """UPDATE leads SET
           reuniao_em='2099-06-20T15:00:00-03:00',
           reuniao_event_id='evt-VIVO',
           reuniao_meet_link='https://meet.google.com/vivo'
           WHERE jurichat_conversation_id='C-1'"""
    )
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript)
    calendar = _make_calendar_confirma()
    # Defere ao Claude — que aqui apenas responde (não remarca).
    triagem_fn = await _triagem_returning(
        Decisao(acao="responder", mensagem="Sua reunião segue marcada!")
    )

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20, calendar=calendar,
    )

    # Signal 1.8 NÃO disparou: não cancelou nem recriou o evento.
    calendar.client.create_event.assert_not_awaited()
    calendar.client.cancel_event.assert_not_awaited()
    triagem_fn_called = jurichat.send_message.call_args_list
    assert any("segue marcada" in c[0][1] for c in triagem_fn_called)
    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["reuniao_event_id"] == "evt-VIVO"  # intacto


@pytest.mark.asyncio
@pytest.mark.parametrize("cenario", ["max_turnos", "handoff", "opt_out"])
async def test_terminal_path_limpa_horarios_oferecidos(db_conn, cenario):
    """S6: caminhos terminais (max_turnos / handoff / opt-out) limpam a coluna
    horarios_oferecidos — não vaza oferta velha pra reativação/Signal 1.8."""
    from noviello_funil.state import set_horarios_oferecidos

    if cenario == "max_turnos":
        transcript = "Lead: ainda na dúvida\nLead: tem mais alguma opção?"
        triagem_fn = AsyncMock(side_effect=AssertionError("não chama Claude"))
    elif cenario == "handoff":
        transcript = "Lead: preciso falar com um atendente humano"
        triagem_fn = await _triagem_returning(
            Decisao(acao="handoff", mensagem="x", motivo_handoff="Lead pediu humano")
        )
    else:  # opt_out
        transcript = "Lead: pode parar de me mandar mensagem, por favor"
        triagem_fn = AsyncMock(side_effect=AssertionError("não chama Claude"))

    _insert_lead_due_for_poll(
        db_conn, transcript_hash="stale",
        turnos=20 if cenario == "max_turnos" else 0,
    )
    lead = get_lead_by_conversation(db_conn, "C-1")
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    ])

    jurichat = _make_jurichat(transcript)

    await run_poll_cycle(
        get_db=lambda: db_conn, jurichat=jurichat, triagem_fn=triagem_fn,
        mario_conversation_id="mario-conv", max_turnos=20,
    )

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert lead["horarios_oferecidos"] is None
