"""D4 (25/jun): integração da sync de reuniões marcadas FORA do bot."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.scheduler import CalendarConfig, sync_reunioes_manuais
from noviello_funil.state import (
    create_lead_if_absent,
    get_lead_by_conversation,
    set_lead_email,
    set_reuniao,
)


def _calendar(eventos):
    client = MagicMock()
    client.list_events = AsyncMock(return_value=eventos)
    return CalendarConfig(
        client=client, business_hours_start=14, business_hours_end=19,
        slot_min=30, buffer_min=0, lookahead_days=5, num_slots=3,
    )


def _jurichat():
    j = MagicMock()
    j.send_message = AsyncMock(return_value={"id": "x"})
    j.start_human_support = AsyncMock(return_value={"success": True})
    return j


def _ev(event_id, *, start_iso, email_externo, summary="Reunião", meet=""):
    attendees = []
    if email_externo:
        attendees.append(
            {"email": email_externo, "self": False, "organizer": False}
        )
    # dono do calendário (Mario) — self/organizer.
    attendees.append(
        {"email": "mario@noviello.adv.br", "self": True, "organizer": True}
    )
    return {
        "id": event_id, "summary": summary, "start_iso": start_iso,
        "meet_link": meet, "attendees": attendees,
    }


def _para_mario(jurichat):
    return [c for c in jurichat.send_message.call_args_list if c.args[0] == "MARIO"]


@pytest.mark.asyncio
async def test_sync_auto_vincula_por_email(db_conn):
    from noviello_funil.state import set_horarios_oferecidos

    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "João")
    set_lead_email(db_conn, lead["id"], "joao@exemplo.com")
    # Oferta pendente ANTES do vínculo manual — o vínculo deve limpá-la
    # (S2 revisto 23/jul: com o 1.8 confirmando remarcação, uma oferta órfã
    # coexistindo com reunião manual deixaria um comentário casual do lead
    # cancelar/recriar a reunião que o Mario marcou na mão).
    set_horarios_oferecidos(db_conn, lead["id"], [
        {"iso": "2099-06-16T14:00:00-03:00", "label": "ter (16/jun) às 14h"},
    ])
    jurichat = _jurichat()
    cal = _calendar([_ev(
        "evt-manual", start_iso="2027-06-26T10:00:00-03:00",
        email_externo="joao@exemplo.com", meet="https://meet.google.com/x",
    )])

    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=cal, jurichat=jurichat,
        mario_conversation_id="MARIO",
    )

    row = get_lead_by_conversation(db_conn, "C-1")
    assert row["reuniao_em"] == "2027-06-26T10:00:00-03:00"
    assert row["reuniao_event_id"] == "evt-manual"
    assert row["reuniao_meet_link"] == "https://meet.google.com/x"
    assert row["horarios_oferecidos"] is None  # oferta pendente consumida
    para = _para_mario(jurichat)
    assert len(para) == 1
    assert "vinculei" in para[0].args[1].lower()


@pytest.mark.asyncio
async def test_sync_ignora_evento_ja_rastreado(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "João")
    set_lead_email(db_conn, lead["id"], "joao@exemplo.com")
    set_reuniao(
        db_conn, lead["id"], reuniao_em_iso="2027-06-20T15:00:00-03:00",
        event_id="evt-bot", meet_link="",
    )
    jurichat = _jurichat()
    cal = _calendar([_ev(
        "evt-bot", start_iso="2027-06-20T15:00:00-03:00",
        email_externo="joao@exemplo.com",
    )])

    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=cal, jurichat=jurichat,
        mario_conversation_id="MARIO",
    )

    assert get_lead_by_conversation(db_conn, "C-1")["reuniao_event_id"] == "evt-bot"
    assert jurichat.send_message.call_args_list == []  # nada tocado


@pytest.mark.asyncio
async def test_sync_conflito_nao_sobrescreve(db_conn):
    lead = create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "João")
    set_lead_email(db_conn, lead["id"], "joao@exemplo.com")
    set_reuniao(
        db_conn, lead["id"], reuniao_em_iso="2027-06-20T15:00:00-03:00",
        event_id="evt-bot", meet_link="",
    )
    jurichat = _jurichat()
    # Evento manual DIFERENTE pro mesmo lead → conflito, não sobrescreve.
    cal = _calendar([_ev(
        "evt-outro", start_iso="2027-06-21T11:00:00-03:00",
        email_externo="joao@exemplo.com",
    )])

    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=cal, jurichat=jurichat,
        mario_conversation_id="MARIO",
    )

    row = get_lead_by_conversation(db_conn, "C-1")
    assert row["reuniao_event_id"] == "evt-bot"  # intocado
    para = _para_mario(jurichat)
    assert len(para) == 1
    assert "conflito" in para[0].args[1].lower()


@pytest.mark.asyncio
async def test_sync_nao_casado_alerta_uma_vez(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Outro")
    cal = _calendar([_ev(
        "evt-x", start_iso="2027-06-26T10:00:00-03:00",
        email_externo="desconhecido@x.com",
    )])

    jurichat = _jurichat()
    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=cal, jurichat=jurichat,
        mario_conversation_id="MARIO",
    )
    para = _para_mario(jurichat)
    assert len(para) == 1
    assert "registre" in para[0].args[1].lower()

    # 2ª passada (mesmo evento) → NÃO re-alerta (dedup).
    jurichat2 = _jurichat()
    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=cal, jurichat=jurichat2,
        mario_conversation_id="MARIO",
    )
    assert _para_mario(jurichat2) == []


@pytest.mark.asyncio
async def test_sync_ignora_allday_e_sem_convidado_externo(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "X")
    jurichat = _jurichat()
    cal = _calendar([
        _ev("evt-allday", start_iso=None, email_externo="x@x.com"),        # all-day
        _ev("evt-audiencia", start_iso="2027-06-26T09:00:00-03:00",
            email_externo=None),                                            # sem convidado
    ])

    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=cal, jurichat=jurichat,
        mario_conversation_id="MARIO",
    )
    assert jurichat.send_message.call_args_list == []  # nenhum tocado


@pytest.mark.asyncio
async def test_sync_off_quando_calendar_ausente(db_conn):
    """Sem calendar configurado → no-op (não quebra)."""
    jurichat = _jurichat()
    await sync_reunioes_manuais(
        get_db=lambda: db_conn, calendar=None, jurichat=jurichat,
        mario_conversation_id="MARIO",
    )
    assert jurichat.send_message.call_args_list == []
