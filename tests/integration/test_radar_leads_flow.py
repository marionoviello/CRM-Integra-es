"""Radar de leads (30/ago, caso Paulo) — alerta de documento + relatório."""

import datetime
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from noviello_funil.radar_leads import run_radar_leads
from noviello_funil.state import (
    CLEAR_PROXIMA_ACAO,
    Estado,
    create_lead_if_absent,
    transicao,
)

TZ = ZoneInfo("America/Sao_Paulo")
# Quarta 26/08/2026 10h BRT — fora dos slots de relatório (9h/15h).
AGORA = datetime.datetime(2026, 8, 26, 10, 0, tzinfo=TZ)


def _iso(dt):
    return dt.astimezone(datetime.UTC).isoformat().replace("+00:00", "Z")


def _jurichat(conversas):
    j = MagicMock()
    j.send_message = AsyncMock(return_value={"id": "x"})
    j.start_human_support = AsyncMock(return_value={"success": True})

    async def _get(conv_id, **_kwargs):
        return {"messages_raw": conversas[conv_id]}

    j.get_conversation = AsyncMock(side_effect=_get)
    return j


def _msgs_para_mario(j):
    return [c.args[1] for c in j.send_message.call_args_list if c.args[0] == "MARIO"]


def _lead_aguardando_humano(db_conn, nome, conv_id):
    lead = create_lead_if_absent(
        db_conn, f"L-{conv_id}", conv_id, "5500000000001", nome,
    )
    transicao(
        db_conn, lead["id"], Estado.AGUARDANDO_HUMANO,
        motivo="humano_assumiu_conversa",
        proxima_acao_horas=CLEAR_PROXIMA_ACAO,
    )
    return lead


async def _rodar(db_conn, jurichat, *, agora=AGORA, varredura_min=0):
    await run_radar_leads(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        mario_conversation_id="MARIO",
        agora=agora,
        varredura_min=varredura_min,
    )


@pytest.mark.asyncio
async def test_documento_2h_sem_resposta_alerta_uma_vez(db_conn):
    """Doc há 3h sem resposta → 🚨 com link do painel; 2ª varredura não repete."""
    _lead_aguardando_humano(db_conn, "Paulo Teste", "C-PAULO")
    conversas = {"C-PAULO": [
        {"direction": "OUTBOUND", "messageAt": _iso(AGORA - datetime.timedelta(days=7)),
         "type": "text", "id": "o1"},
        {"direction": "INBOUND", "messageAt": _iso(AGORA - datetime.timedelta(hours=3)),
         "type": "document", "id": "doc-paulo"},
    ]}
    jurichat = _jurichat(conversas)

    await _rodar(db_conn, jurichat)
    avisos = _msgs_para_mario(jurichat)
    assert len(avisos) == 1
    assert "URGENTE" in avisos[0]
    assert "3h" in avisos[0]
    assert "app.jurichat.com/messages?id=C-PAULO" in avisos[0]

    await _rodar(db_conn, jurichat)
    assert len(_msgs_para_mario(jurichat)) == 1  # idempotente por documento


@pytest.mark.asyncio
async def test_documento_recente_nao_alerta(db_conn):
    _lead_aguardando_humano(db_conn, "Ana Teste", "C-ANA")
    conversas = {"C-ANA": [
        {"direction": "INBOUND", "messageAt": _iso(AGORA - datetime.timedelta(hours=1)),
         "type": "image", "id": "doc-ana"},
    ]}
    jurichat = _jurichat(conversas)
    await _rodar(db_conn, jurichat)
    assert _msgs_para_mario(jurichat) == []


@pytest.mark.asyncio
async def test_relatorio_no_slot_9h_e_sem_duplicata(db_conn):
    """Às 9h05 sai o relatório com o lead mais parado; 2ª chamada não duplica."""
    _lead_aguardando_humano(db_conn, "Paulo Teste", "C-PAULO")
    agora = AGORA.replace(hour=9, minute=5)
    conversas = {"C-PAULO": [
        {"direction": "OUTBOUND", "messageAt": _iso(agora - datetime.timedelta(days=12)),
         "type": "text", "id": "o1"},
        {"direction": "INBOUND", "messageAt": _iso(agora - datetime.timedelta(days=11)),
         "type": "text", "id": "i1"},
    ]}
    jurichat = _jurichat(conversas)

    await _rodar(db_conn, jurichat, agora=agora)
    avisos = _msgs_para_mario(jurichat)
    assert len(avisos) == 1
    assert "Radar de leads" in avisos[0]
    assert "Paulo Teste" in avisos[0]
    assert "11d" in avisos[0]
    assert "com humano" in avisos[0]

    await _rodar(db_conn, jurichat, agora=agora.replace(minute=35))
    assert len(_msgs_para_mario(jurichat)) == 1  # cooldown de 12h do slot


@pytest.mark.asyncio
async def test_fora_do_slot_sem_relatorio(db_conn):
    _lead_aguardando_humano(db_conn, "Paulo Teste", "C-PAULO")
    conversas = {"C-PAULO": [
        {"direction": "INBOUND", "messageAt": _iso(AGORA - datetime.timedelta(days=2)),
         "type": "text", "id": "i1"},
    ]}
    jurichat = _jurichat(conversas)
    await _rodar(db_conn, jurichat)  # 10h — fora de 9/15
    assert _msgs_para_mario(jurichat) == []


@pytest.mark.asyncio
async def test_baseline_e_canal_de_alerta_ficam_fora(db_conn):
    """Motivo baseline_first_sync e o próprio canal de alerta nem são lidos."""
    lead_base = create_lead_if_absent(
        db_conn, "L-BASE", "C-BASE", "5500000000002", "Carteira Antiga",
    )
    transicao(
        db_conn, lead_base["id"], Estado.AGUARDANDO_HUMANO,
        motivo="baseline_first_sync", proxima_acao_horas=CLEAR_PROXIMA_ACAO,
    )
    _lead_aguardando_humano(db_conn, "Canal Mario", "MARIO")
    jurichat = _jurichat({})  # qualquer fetch estouraria KeyError
    await _rodar(db_conn, jurichat)
    jurichat.get_conversation.assert_not_awaited()


@pytest.mark.asyncio
async def test_gate_de_varredura_segura_o_ritmo(db_conn):
    _lead_aguardando_humano(db_conn, "Paulo Teste", "C-PAULO")
    conversas = {"C-PAULO": [
        {"direction": "INBOUND", "messageAt": _iso(AGORA - datetime.timedelta(hours=5)),
         "type": "text", "id": "i1"},
    ]}
    jurichat = _jurichat(conversas)
    await _rodar(db_conn, jurichat, varredura_min=60)
    assert jurichat.get_conversation.await_count == 1
    await _rodar(db_conn, jurichat, varredura_min=60)
    assert jurichat.get_conversation.await_count == 1  # cooldown segurou
