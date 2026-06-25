"""Integration tests for run_reminder_cycle (lembretes 24h/2h/30min)."""

import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import httpx
import pytest

from noviello_funil.scheduler import run_reminder_cycle
from noviello_funil.state import Estado, set_reuniao

TZ = ZoneInfo("America/Sao_Paulo")


def _insert_lead(conn, *, nome="Maria"):
    conn.execute(
        """INSERT INTO leads
           (jurichat_lead_id, jurichat_conversation_id, contato_telefone,
            contato_nome, estado)
           VALUES ('L-1', 'C-1', '5511999999999', ?, ?)""",
        (nome, Estado.EM_CONVERSA),
    )
    return conn.execute(
        "SELECT id FROM leads WHERE jurichat_lead_id = 'L-1'"
    ).fetchone()["id"]


def _make_jurichat():
    fake = MagicMock()
    fake.send_message = AsyncMock(return_value={"id": "msg-x"})
    fake.start_human_support = AsyncMock(return_value={"success": True})
    # Sem cancelamento por padrão — a última fala do lead não pede cancelar.
    fake.get_conversation = AsyncMock(
        return_value={"transcription": "Lead: oi\nAtendente: ola"}
    )
    return fake


# --- Cases ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_lembrete_cancela_quando_lead_pediu_cancelamento(db_conn):
    """BUG Daniel (19/jun): lead com reunião agendada que o brain NÃO
    reprocessa (modo-humano / agendado manual) pede cancelamento. O ciclo NÃO
    deve mandar o lembrete — deve cancelar a reunião (limpa reuniao_em → para
    os lembretes) e avisar o Mario, sem mensagem ao lead."""
    lead_id = _insert_lead(db_conn, nome="Daniel")
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=20)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/x', estado=?,
           lembrete_24h_enviado_em=datetime('now','-23 hours'),
           lembrete_2h_enviado_em=datetime('now','-2 hours')
           WHERE id=?""",
        (reuniao.isoformat(), Estado.AGUARDANDO_HUMANO, lead_id),
    )
    jurichat = _make_jurichat()
    jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Atendente: em 30 min começa...\nLead: Cancela por gentileza",
    })

    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat,
        mario_conversation_id="MARIO-1",
    )

    # NÃO mandou lembrete pro lead (conversa C-1).
    para_lead = [c for c in jurichat.send_message.call_args_list if c.args[0] == "C-1"]
    assert para_lead == [], f"não devia mandar lembrete ao lead: {para_lead}"
    # Reunião limpa → não dispara mais.
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["reuniao_em"] is None
    assert lead["lembrete_30min_enviado_em"] is None
    # Mario foi avisado (mensagem pra MARIO-1).
    para_mario = [c for c in jurichat.send_message.call_args_list if c.args[0] == "MARIO-1"]
    assert len(para_mario) >= 1
    assert "cancel" in para_mario[0].args[1].lower()


@pytest.mark.asyncio
async def test_lembrete_dispara_normal_se_checagem_cancelamento_falha(db_conn):
    """Robustez: se a checagem de cancelamento falha (Jurichat fora do ar), o
    ciclo NÃO engole a reunião — degrada e manda o lembrete normalmente."""
    lead_id = _insert_lead(db_conn, nome="Maria")
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=20)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/x',
           lembrete_24h_enviado_em=datetime('now','-23 hours'),
           lembrete_2h_enviado_em=datetime('now','-2 hours')
           WHERE id=?""",
        (reuniao.isoformat(), lead_id),
    )
    jurichat = _make_jurichat()
    jurichat.get_conversation = AsyncMock(side_effect=RuntimeError("jurichat down"))

    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    para_lead = [c for c in jurichat.send_message.call_args_list if c.args[0] == "C-1"]
    assert len(para_lead) == 1  # mandou o lembrete mesmo com a checagem falhando
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["lembrete_30min_enviado_em"] is not None
    assert lead["reuniao_em"] is not None  # NÃO limpou a reunião


@pytest.mark.asyncio
async def test_reminder_5min_dispara_com_aviso_de_cancelamento(db_conn):
    """Lembrete de 5 min antes: dispara quando faltam <5min (mais novo da fila)
    e inclui o aviso de cancelamento por no-show."""
    lead_id = _insert_lead(db_conn, nome="Joao")
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=3)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/abc',
           lembrete_24h_enviado_em=datetime('now','-23 hours'),
           lembrete_2h_enviado_em=datetime('now','-2 hours'),
           lembrete_30min_enviado_em=datetime('now','-25 minutes')
           WHERE id=?""",
        (reuniao.isoformat(), lead_id),
    )
    jurichat = _make_jurichat()
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    jurichat.send_message.assert_awaited_once()
    msg = jurichat.send_message.call_args.args[1]
    assert "5 minutos" in msg
    assert "https://meet.google.com/abc" in msg
    assert "cancelada" in msg.lower()  # o aviso de no-show
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["lembrete_5min_enviado_em"] is not None


@pytest.mark.asyncio
async def test_noshow_ping_dispara_5min_apos_inicio_uma_vez(db_conn):
    """No-show: 5 min APÓS o início, sem ter avisado, o ciclo PINGA o Mario com
    o link de cancelamento (grava o token = avisa uma vez só), sem msg ao lead."""
    lead_id = _insert_lead(db_conn, nome="Pedro")
    reuniao = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=6)
    db_conn.execute(
        "UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1' WHERE id=?",
        (reuniao.isoformat(), lead_id),
    )
    jurichat = _make_jurichat()
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    para_mario = [c for c in jurichat.send_message.call_args_list if c.args[0] == "MARIO-1"]
    assert len(para_mario) == 1
    msg = para_mario[0].args[1]
    assert "no-show" in msg.lower()
    assert "/reuniao/cancelar/" in msg
    para_lead = [c for c in jurichat.send_message.call_args_list if c.args[0] == "C-1"]
    assert para_lead == []
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["noshow_token"] is not None

    # Segundo tick: NÃO pinga de novo (token já setado).
    jurichat2 = _make_jurichat()
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat2,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    assert jurichat2.send_message.call_args_list == []


@pytest.mark.asyncio
async def test_noshow_ping_falha_no_envio_nao_marca_token_e_retenta(db_conn):
    """D3 (auditoria 24/jun): se o envio ao Mario falhar, o token de no-show NÃO
    é gravado → o próximo tick re-tenta o ping em vez de perder o alerta pra
    sempre. Antes o token era marcado ANTES de enviar e a falha sumia com o
    aviso (a condição de re-ping exige noshow_token IS NULL)."""
    lead_id = _insert_lead(db_conn, nome="Pedro")
    reuniao = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=6)
    db_conn.execute(
        "UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1' WHERE id=?",
        (reuniao.isoformat(), lead_id),
    )

    # 1º tick: envio ao Mario FALHA (RequestError é engolido pelo notify_mario,
    # que agora retorna False).
    jurichat = _make_jurichat()
    jurichat.send_message = AsyncMock(side_effect=httpx.RequestError("down"))
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["noshow_token"] is None, "token não pode ser gravado se o envio falhou"

    # 2º tick: envio OK → pinga e AGORA grava o token (alerta não se perdeu).
    jurichat2 = _make_jurichat()
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat2,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    para_mario = [
        c for c in jurichat2.send_message.call_args_list if c.args[0] == "MARIO-1"
    ]
    assert len(para_mario) == 1
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["noshow_token"] is not None


@pytest.mark.asyncio
async def test_reuniao_em_invalido_e_limpa_e_alerta_mario(db_conn):
    """D5 (auditoria 24/jun): reunião com reuniao_em inparseável (fantasma) é
    LIMPA + alerta o Mario, em vez de ficar presa pra sempre no ciclo (sem
    lembrete, sem no-show, nunca removida)."""
    lead_id = _insert_lead(db_conn, nome="Fantasma")
    # reuniao_em lixo (não passa pelo set_reuniao, que agora rejeita) — simula
    # dado legado ou import manual quebrado.
    db_conn.execute(
        "UPDATE leads SET reuniao_em='horario-quebrado', reuniao_event_id='evt-x' "
        "WHERE id=?",
        (lead_id,),
    )
    jurichat = _make_jurichat()
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    # Reunião fantasma removida.
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["reuniao_em"] is None
    # Mario foi alertado (nada ao lead).
    para_mario = [
        c for c in jurichat.send_message.call_args_list if c.args[0] == "MARIO-1"
    ]
    assert len(para_mario) == 1
    assert "inválido" in para_mario[0].args[1].lower()
    para_lead = [
        c for c in jurichat.send_message.call_args_list if c.args[0] == "C-1"
    ]
    assert para_lead == []


@pytest.mark.asyncio
async def test_reuniao_em_invalido_nao_limpa_se_alerta_falha(db_conn):
    """D5 (revisão adversarial 24/jun): se o alerta ao Mario falhar, o fantasma
    NÃO é limpo — senão (autocommit + Jurichat fora) o lead sumiria do ciclo e
    o aviso de uma reunião real se perderia pra sempre. Espelha o D3."""
    lead_id = _insert_lead(db_conn, nome="Fantasma")
    db_conn.execute(
        "UPDATE leads SET reuniao_em='horario-quebrado' WHERE id=?", (lead_id,),
    )
    # 1º tick: alerta FALHA (RequestError engolido por notify_mario → False).
    jurichat = _make_jurichat()
    jurichat.send_message = AsyncMock(side_effect=httpx.RequestError("down"))
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["reuniao_em"] == "horario-quebrado", "não pode limpar sem avisar"

    # 2º tick: alerta OK → agora alerta e limpa.
    jurichat2 = _make_jurichat()
    await run_reminder_cycle(
        get_db=lambda: db_conn, jurichat=jurichat2,
        mario_conversation_id="MARIO-1", base_url="https://funil.example",
    )
    para_mario = [
        c for c in jurichat2.send_message.call_args_list if c.args[0] == "MARIO-1"
    ]
    assert len(para_mario) == 1
    lead = db_conn.execute("SELECT * FROM leads WHERE id=?", (lead_id,)).fetchone()
    assert lead["reuniao_em"] is None


@pytest.mark.asyncio
async def test_reminder_30min_dispara_quando_falta_menos_de_30min(db_conn):
    """Cenário: reunião marcada com antecedência (24h+), tempo passou,
    agora faltam 20 min. Cycle deve disparar 30min."""
    lead_id = _insert_lead(db_conn)
    # Reunião em 20 min — mas simula que foi marcada quando faltavam 25h
    # (todos os flags estão NULL).
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=20)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/abc-defg-hij',
           lembrete_24h_enviado_em=datetime('now', '-23 hours'),
           lembrete_2h_enviado_em=datetime('now', '-2 hours')
           WHERE id = ?""",
        (reuniao.isoformat(), lead_id),
    )

    jurichat = _make_jurichat()
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    jurichat.send_message.assert_awaited_once()
    msg = jurichat.send_message.call_args[0][1]
    assert "30 minutos" in msg or "videochamada" in msg
    assert "https://meet.google.com/abc-defg-hij" in msg

    # Flag de 30min marcada
    lead = db_conn.execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert lead["lembrete_30min_enviado_em"] is not None


@pytest.mark.asyncio
async def test_reminder_24h_dispara_para_reuniao_em_23h(db_conn):
    """Reunião em 23h, marcada anteriormente (todos flags NULL).
    Cycle deve mandar APENAS o 24h."""
    lead_id = _insert_lead(db_conn)
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=23)
    # Marca reunião sem usar set_reuniao (pra não pré-marcar 24h flag)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/x' WHERE id=?""",
        (reuniao.isoformat(), lead_id),
    )

    jurichat = _make_jurichat()
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    jurichat.send_message.assert_awaited_once()
    msg = jurichat.send_message.call_args[0][1]
    assert "amanhã" in msg.lower() or "amanha" in msg.lower()
    # 24h sempre menciona remarcação ("Se precisar remarcar")
    assert "remarcar" in msg.lower() or "dúvida" in msg.lower()
    lead = db_conn.execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert lead["lembrete_24h_enviado_em"] is not None
    # 2h e 30min ainda não disparados (delta > 2h)
    assert lead["lembrete_2h_enviado_em"] is None
    assert lead["lembrete_30min_enviado_em"] is None


@pytest.mark.asyncio
async def test_reminder_nao_dispara_duas_vezes(db_conn):
    """Tick subsequente NÃO reenvia o mesmo lembrete."""
    lead_id = _insert_lead(db_conn)
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=10)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/x',
           lembrete_24h_enviado_em=datetime('now', '-23 hours'),
           lembrete_2h_enviado_em=datetime('now', '-2 hours')
           WHERE id = ?""",
        (reuniao.isoformat(), lead_id),
    )

    jurichat = _make_jurichat()
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    # Mesmo após 3 ticks, manda 1 vez só (30min flag protege).
    assert jurichat.send_message.await_count == 1


@pytest.mark.asyncio
async def test_reminder_reuniao_passada_limpa_dados(db_conn):
    """Reunião que já passou → clear_reuniao (sem mandar lembrete)."""
    lead_id = _insert_lead(db_conn)
    # Reunião 1 hora atrás
    reuniao = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    # Insere bypassing o set_reuniao (que não aceita passado fácil).
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-x',
           reuniao_meet_link='https://m.com/x' WHERE id=?""",
        (reuniao.isoformat(), lead_id),
    )

    jurichat = _make_jurichat()
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    jurichat.send_message.assert_not_awaited()
    lead = db_conn.execute(
        "SELECT reuniao_em FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert lead["reuniao_em"] is None  # foi limpa


@pytest.mark.asyncio
async def test_set_reuniao_premarca_lembretes_perdidos(db_conn):
    """Reunião marcada com <30min de antecedência → 30min/2h/24h vão
    como pré-enviados (evita lembretes desnecessários)."""
    lead_id = _insert_lead(db_conn)
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=20)
    set_reuniao(
        db_conn, lead_id,
        reuniao_em_iso=reuniao.isoformat(),
        event_id="evt-x", meet_link="https://m.com",
    )

    lead = db_conn.execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    # Todos os 3 foram pré-marcados como enviados pq delta=20min cabe
    # em qualquer janela.
    assert lead["lembrete_24h_enviado_em"] is not None
    assert lead["lembrete_2h_enviado_em"] is not None
    assert lead["lembrete_30min_enviado_em"] is not None


@pytest.mark.asyncio
async def test_lembrete_com_falha_de_envio_nao_marca_flag(db_conn):
    """Auditoria 2026-06-11: flag era marcada mesmo com envio falho —
    lembrete perdido pra sempre. Agora re-tenta no próximo tick."""
    lead_id = _insert_lead(db_conn)
    reuniao = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=20)
    db_conn.execute(
        """UPDATE leads SET reuniao_em=?, reuniao_event_id='evt-1',
           reuniao_meet_link='https://meet.google.com/x',
           lembrete_24h_enviado_em=datetime('now', '-23 hours'),
           lembrete_2h_enviado_em=datetime('now', '-2 hours')
           WHERE id = ?""",
        (reuniao.isoformat(), lead_id),
    )

    jurichat = MagicMock()
    jurichat.start_human_support = AsyncMock(return_value={"success": True})
    jurichat.send_message = AsyncMock(side_effect=RuntimeError("WhatsApp down"))

    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)

    lead = db_conn.execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert lead["lembrete_30min_enviado_em"] is None  # NÃO marcado

    # WhatsApp volta → próximo tick reenvia e marca
    jurichat.send_message = AsyncMock(return_value={"id": "m"})
    await run_reminder_cycle(get_db=lambda: db_conn, jurichat=jurichat)
    lead = db_conn.execute(
        "SELECT * FROM leads WHERE id = ?", (lead_id,)
    ).fetchone()
    assert lead["lembrete_30min_enviado_em"] is not None
