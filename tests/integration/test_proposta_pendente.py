"""Prévia de proposta pós-reunião (05/set, caso Kayan).

Reunião realizada (3h+ atrás, até 7 dias) sem contrato vivo no pipeline →
prévia interna do orçamento vai pros canais de alerta, 1× por reunião.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.scheduler import run_proposta_pendente_cycle
from noviello_funil.state import create_lead_if_absent

AGORA = datetime.datetime(2026, 9, 5, 15, 0, tzinfo=datetime.UTC)


def _lead_com_reuniao(db_conn, conv_id, *, horas_atras):
    lead = create_lead_if_absent(
        db_conn, f"L-{conv_id}", conv_id, "5500000000001", "Fulano Teste",
    )
    quando = (AGORA - datetime.timedelta(hours=horas_atras)).isoformat()
    db_conn.execute(
        "UPDATE leads SET reuniao_em = ?, reuniao_event_id = 'evt-1' "
        "WHERE id = ?",
        (quando, lead["id"]),
    )
    db_conn.commit()
    return lead


def _jurichat():
    j = MagicMock()
    j.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: reunião foi ótima, aguardo a proposta",
    })
    j.send_message = AsyncMock(return_value={"id": "x"})
    j.start_human_support = AsyncMock(return_value={"success": True})
    return j


def _msgs_para_mario(j):
    return [c.args[1] for c in j.send_message.call_args_list if c.args[0] == "MARIO"]


async def _rodar(db_conn, jurichat, gerar_previa=None):
    async def _previa_padrao(**_kwargs):
        return "PRÉVIA DE TESTE: escopo X. Honorários sugeridos: R$ ____."

    await run_proposta_pendente_cycle(
        get_db=lambda: db_conn,
        jurichat=jurichat,
        gerar_previa=gerar_previa or _previa_padrao,
        mario_conversation_id="MARIO",
        agora=AGORA,
    )


@pytest.mark.asyncio
async def test_reuniao_sem_proposta_manda_previa_uma_vez(db_conn):
    _lead_com_reuniao(db_conn, "C-1", horas_atras=5)
    jurichat = _jurichat()

    await _rodar(db_conn, jurichat)
    avisos = _msgs_para_mario(jurichat)
    assert len(avisos) == 1
    assert "Proposta pendente" in avisos[0]
    assert "PRÉVIA DE TESTE" in avisos[0]
    assert "app.jurichat.com/messages?id=C-1" in avisos[0]

    await _rodar(db_conn, jurichat)
    assert len(_msgs_para_mario(jurichat)) == 1  # 1 aviso por reunião


@pytest.mark.asyncio
async def test_reuniao_recente_ou_futura_nao_dispara(db_conn):
    _lead_com_reuniao(db_conn, "C-1", horas_atras=1)    # acabou de acontecer
    _lead_com_reuniao(db_conn, "C-2", horas_atras=-24)  # ainda vai acontecer
    _lead_com_reuniao(db_conn, "C-3", horas_atras=24 * 10)  # velha demais
    jurichat = _jurichat()
    await _rodar(db_conn, jurichat)
    assert _msgs_para_mario(jurichat) == []


@pytest.mark.asyncio
async def test_contrato_vivo_no_pipeline_nao_dispara(db_conn):
    lead = _lead_com_reuniao(db_conn, "C-1", horas_atras=5)
    db_conn.execute(
        "INSERT INTO contrato (lead_id, cliente_nome, valor_honorarios, estado)"
        " VALUES (?, 'Fulano Teste', 'R$ 1', 'contrato_enviado')",
        (lead["id"],),
    )
    db_conn.commit()
    jurichat = _jurichat()
    await _rodar(db_conn, jurichat)
    assert _msgs_para_mario(jurichat) == []


@pytest.mark.asyncio
async def test_contrato_recusado_nao_bloqueia(db_conn):
    lead = _lead_com_reuniao(db_conn, "C-1", horas_atras=5)
    db_conn.execute(
        "INSERT INTO contrato (lead_id, cliente_nome, valor_honorarios, estado)"
        " VALUES (?, 'Fulano Teste', 'R$ 1', 'contrato_recusado')",
        (lead["id"],),
    )
    db_conn.commit()
    jurichat = _jurichat()
    await _rodar(db_conn, jurichat)
    assert len(_msgs_para_mario(jurichat)) == 1  # recusado não conta como vivo


@pytest.mark.asyncio
async def test_falha_na_previa_ainda_avisa(db_conn):
    _lead_com_reuniao(db_conn, "C-1", horas_atras=5)
    jurichat = _jurichat()

    async def _previa_quebrada(**_kwargs):
        raise RuntimeError("API fora do ar")

    await _rodar(db_conn, jurichat, gerar_previa=_previa_quebrada)
    avisos = _msgs_para_mario(jurichat)
    assert len(avisos) == 1
    assert "indisponível" in avisos[0]  # avisa mesmo sem a prévia
