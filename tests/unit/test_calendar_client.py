"""Tests for the Google Calendar client."""

import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from noviello_funil.calendar_client import (
    GoogleCalendarClient,
    Slot,
    _overlaps_any,
)

TZ = ZoneInfo("America/Sao_Paulo")


def _dt(year, month, day, hour, minute=0):
    return datetime.datetime(year, month, day, hour, minute, tzinfo=TZ)


# --- Slot helpers ---------------------------------------------------------

def test_slot_end_computed_from_duration():
    s = Slot(start=_dt(2026, 6, 9, 14, 0), duration_min=30)
    assert s.end == _dt(2026, 6, 9, 14, 30)


def test_slot_format_human_no_minutes():
    # 2026-06-09 é terça-feira
    s = Slot(start=_dt(2026, 6, 9, 14, 0), duration_min=30)
    assert s.format_human() == "ter (09/jun) às 14h"


def test_slot_format_human_with_minutes():
    s = Slot(start=_dt(2026, 6, 10, 15, 30), duration_min=30)
    # 2026-06-10 é quarta
    assert s.format_human() == "qua (10/jun) às 15h30"


# --- _overlaps_any --------------------------------------------------------

def test_overlap_detects_intersection():
    slot_a = _dt(2026, 6, 9, 14, 0)
    slot_b = _dt(2026, 6, 9, 14, 30)
    busy = [(_dt(2026, 6, 9, 14, 15), _dt(2026, 6, 9, 15, 0))]
    assert _overlaps_any(slot_a, slot_b, busy) is True


def test_overlap_back_to_back_does_not_collide():
    """Slot [14h-14h30] adjacente a busy [14h30-15h] NÃO sobrepõe."""
    slot_a = _dt(2026, 6, 9, 14, 0)
    slot_b = _dt(2026, 6, 9, 14, 30)
    busy = [(_dt(2026, 6, 9, 14, 30), _dt(2026, 6, 9, 15, 0))]
    assert _overlaps_any(slot_a, slot_b, busy) is False


def test_overlap_empty_busy():
    slot_a = _dt(2026, 6, 9, 14, 0)
    slot_b = _dt(2026, 6, 9, 14, 30)
    assert _overlaps_any(slot_a, slot_b, []) is False


# --- OAuth refresh --------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_access_token_caches_until_expiry(respx_mock):
    """Primeira chamada faz POST /token; segunda usa cache."""
    route = respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={
            "access_token": "tok-1",
            "expires_in": 3600,
            "token_type": "Bearer",
        }),
    )

    client = GoogleCalendarClient(
        client_id="cid", client_secret="sec", refresh_token="rt",
    )
    try:
        t1 = await client._get_access_token()
        t2 = await client._get_access_token()
    finally:
        await client.aclose()

    assert t1 == "tok-1"
    assert t2 == "tok-1"
    assert route.call_count == 1


@pytest.mark.asyncio
async def test_authed_request_retries_on_401(respx_mock):
    """Token revogado mid-flight → refresh + retry transparente."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        side_effect=[
            httpx.Response(200, json={"access_token": "tok-old", "expires_in": 3600}),
            httpx.Response(200, json={"access_token": "tok-new", "expires_in": 3600}),
        ],
    )
    route = respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(side_effect=[
        httpx.Response(401, json={"error": "invalid_token"}),
        httpx.Response(200, json={"calendars": {"primary": {"busy": []}}}),
    ])

    client = GoogleCalendarClient(
        client_id="cid", client_secret="sec", refresh_token="rt",
    )
    try:
        await client._fetch_busy_intervals(
            start=_dt(2026, 6, 9, 14, 0),
            end=_dt(2026, 6, 9, 19, 0),
        )
    finally:
        await client.aclose()

    assert route.call_count == 2


# --- find_available_slots -------------------------------------------------

@pytest.mark.asyncio
async def test_find_slots_estrategia_escassez_2_1_1(respx_mock):
    """Estratégia escassez (Mario 2026-06-10): 2 slots do primeiro dia
    (primeiro + último), 1 do dia seguinte, 1 do próximo — em vez de
    N consecutivos que parecem agenda vazia."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))

    # 2026-06-09 = terça 10h da manhã
    now = _dt(2026, 6, 9, 10, 0)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=4, now=now,
        )
    finally:
        await client.aclose()

    # Calendar vazio: ter 14h (primeiro) + ter 18h30 (último do dia)
    # + qua 14h + qui 14h
    assert len(slots) == 4
    assert slots[0].start == _dt(2026, 6, 9, 14, 0)
    assert slots[1].start == _dt(2026, 6, 9, 18, 30)
    assert slots[2].start == _dt(2026, 6, 10, 14, 0)
    assert slots[3].start == _dt(2026, 6, 11, 14, 0)


@pytest.mark.asyncio
async def test_find_slots_escassez_dia1_com_um_slot_so(respx_mock):
    """Dia 1 quase lotado (1 slot livre) → oferece esse 1 + dias seguintes."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    # Terça ocupada das 14h às 18h30 — sobra só o slot 18h30-19h
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": [
            {"start": "2026-06-09T14:00:00-03:00",
             "end":   "2026-06-09T18:30:00-03:00"},
        ]}}},
    ))

    now = _dt(2026, 6, 9, 10, 0)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=4, now=now,
        )
    finally:
        await client.aclose()

    # Dia 1 contribui 1 só (sem duplicar) + qua 14h + qui 14h
    assert len(slots) == 3
    assert slots[0].start == _dt(2026, 6, 9, 18, 30)
    assert slots[1].start == _dt(2026, 6, 10, 14, 0)
    assert slots[2].start == _dt(2026, 6, 11, 14, 0)


@pytest.mark.asyncio
async def test_find_slots_filtra_dias_e_periodo_da_preferencia(respx_mock):
    """Caso José Lucas (03/ago): lead pediu 'terça ou quarta À TARDE' e o
    gerador devolvia seg/manhãs (não tinha COMO saber da preferência). Com
    ``permitir_dias`` + ``periodo``, só saem slots que respeitam o pedido."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))

    # 2026-08-03 = segunda 08h; manhã 9-11 LIGADA (pra provar que o filtro
    # de período derruba a janela da manhã mesmo disponível).
    now = _dt(2026, 8, 3, 8, 0)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=4,
            morning_start=9, morning_end=11,
            permitir_dias={1, 2},  # ter, qua
            periodo="tarde",
            now=now,
        )
    finally:
        await client.aclose()

    # ter (04) 14h + ter 18h30 (primeiro+último do dia 1) + qua (05) 14h +
    # ter (11) 14h — pular dia não-pedido NÃO gasta o lookahead, então a
    # busca alcança a semana seguinte dentro da preferência.
    assert len(slots) == 4
    assert slots[0].start == _dt(2026, 8, 4, 14, 0)
    assert slots[1].start == _dt(2026, 8, 4, 18, 30)
    assert slots[2].start == _dt(2026, 8, 5, 14, 0)
    assert slots[3].start == _dt(2026, 8, 11, 14, 0)
    for s in slots:
        assert s.start.weekday() in (1, 2)
        assert s.start.hour >= 12


@pytest.mark.asyncio
async def test_find_slots_periodo_manha_exclui_a_tarde(respx_mock):
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))

    now = _dt(2026, 8, 3, 8, 0)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=4,
            morning_start=9, morning_end=11,
            periodo="manha",
            now=now,
        )
    finally:
        await client.aclose()

    assert slots
    for s in slots:
        assert s.start.hour < 12


@pytest.mark.asyncio
async def test_find_slots_skips_busy_intervals(respx_mock):
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    # Mario ocupado das 14h às 15h
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": [
            {"start": "2026-06-09T17:00:00-03:00",
             "end":   "2026-06-09T18:00:00-03:00"},
        ]}}},
    ))

    now = _dt(2026, 6, 9, 10, 0)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=3, now=now,
        )
    finally:
        await client.aclose()

    starts = {s.start for s in slots}
    assert _dt(2026, 6, 9, 17, 0) not in starts
    assert _dt(2026, 6, 9, 17, 30) not in starts


@pytest.mark.asyncio
async def test_find_slots_skips_weekends(respx_mock):
    """Sexta 18h → próximos slots pulam sáb/dom e vão pra segunda."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))

    # 2026-06-12 = sexta 18h30 — só sobra 1 slot de hoje (mas requer >=30min
    # de antecedência então só 18h30 está fora; nada mais hoje). Próximo
    # dia útil = segunda 15/06.
    now = _dt(2026, 6, 12, 18, 30)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=3, now=now,
        )
    finally:
        await client.aclose()

    # Sexta 18h30 - nenhum slot hoje (precisa >30min antecedência).
    # Próximos 3 slots de segunda 15/06: 14h, 14h30, 15h
    assert len(slots) == 3
    for s in slots:
        assert s.start.weekday() < 5  # nenhum em sáb/dom
    assert slots[0].start == _dt(2026, 6, 15, 14, 0)


@pytest.mark.asyncio
async def test_find_slots_requires_30min_anticipation(respx_mock):
    """Se now=14h00, slot das 14h não pode ser oferecido (sem tempo)."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))

    now = _dt(2026, 6, 9, 14, 0)  # terça 14h em ponto
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=3, now=now,
        )
    finally:
        await client.aclose()

    # Primeiro slot deve ser >= 14h30 (30min de antecedência)
    assert slots[0].start >= _dt(2026, 6, 9, 14, 30)


# --- create_event ---------------------------------------------------------

@pytest.mark.asyncio
async def test_create_event_sem_email_nao_inclui_attendees_nem_meet(respx_mock):
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    route = respx_mock.post(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    ).mock(return_value=httpx.Response(
        200, json={"id": "evt-1", "status": "confirmed"},
    ))

    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        result = await client.create_event(
            start=_dt(2026, 6, 9, 14, 30),
            duration_min=30,
            lead_nome="José Silva",
            lead_telefone="5511915469015",
            resumo_caso="Inventário, pai faleceu há 20 dias, 3 herdeiros",
        )
    finally:
        await client.aclose()

    assert result["id"] == "evt-1"
    body = route.calls.last.request.read().decode()
    assert "José Silva" in body
    assert "5511915469015" in body
    assert "wa.me/5511915469015" in body
    assert "Inventário" in body
    assert "[Atendimento] José Silva" in body
    # Sem email → sem attendees, sem conferenceData
    assert "attendees" not in body
    assert "conferenceData" not in body
    # Query string sem conferenceDataVersion
    qs = route.calls.last.request.url.query.decode()
    assert "conferenceDataVersion" not in qs
    assert "sendUpdates" not in qs


@pytest.mark.asyncio
async def test_create_event_com_email_adiciona_attendee_e_pede_meet(respx_mock):
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    route = respx_mock.post(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    ).mock(return_value=httpx.Response(200, json={
        "id": "evt-2",
        "status": "confirmed",
        "hangoutLink": "https://meet.google.com/abc-defg-hij",
    }))

    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        result = await client.create_event(
            start=_dt(2026, 6, 9, 14, 30),
            duration_min=30,
            lead_nome="José Silva",
            lead_telefone="5511915469015",
            resumo_caso="Inventário",
            lead_email="jose@exemplo.com",
        )
    finally:
        await client.aclose()

    body = route.calls.last.request.read().decode()
    assert "attendees" in body
    assert "jose@exemplo.com" in body
    assert "conferenceData" in body
    assert "hangoutsMeet" in body
    qs = route.calls.last.request.url.query.decode()
    assert "conferenceDataVersion=1" in qs
    assert "sendUpdates=all" in qs
    # Response devolve o Meet link
    assert result["hangoutLink"] == "https://meet.google.com/abc-defg-hij"


@pytest.mark.asyncio
async def test_cancel_event_passa_sendUpdates_all(respx_mock):
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    route = respx_mock.delete(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/evt-1",
    ).mock(return_value=httpx.Response(204))

    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        await client.cancel_event("evt-1")
    finally:
        await client.aclose()

    assert route.called
    qs = route.calls.last.request.url.query.decode()
    assert "sendUpdates=all" in qs


@pytest.mark.asyncio
async def test_cancel_event_swallows_404(respx_mock):
    """Evento já apagado (manualmente) → 404 não levanta."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.delete(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events/evt-old",
    ).mock(return_value=httpx.Response(404, json={"error": "Not Found"}))

    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        await client.cancel_event("evt-old")  # MUST NOT raise
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_freebusy_com_errors_levanta_excecao(respx_mock):
    """Auditoria 2026-06-11: bloco 'errors' do freeBusy era ignorado →
    agenda lida como 100% livre → double-booking."""
    from noviello_funil.calendar_client import GoogleCalendarError
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(200, json={
        "calendars": {"primary": {
            "errors": [{"domain": "global", "reason": "notFound"}],
            "busy": [],
        }},
    }))

    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        with pytest.raises(GoogleCalendarError):
            await client.find_available_slots(
                business_hours_start=14, business_hours_end=19,
                slot_min=30, buffer_min=0,
                lookahead_days=5, num_slots=4,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_find_slots_inclui_manha_quando_janela_ligada(respx_mock):
    """G2 (2026-06-16): com janela de manhã (10-12h), o 1º slot do dia é de
    manhã (10h) — o gerador oferece manhã + tarde, em ordem crescente."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))
    now = _dt(2026, 6, 9, 8, 0)  # terça 8h — antes da janela da manhã
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=4, now=now,
            morning_start=10, morning_end=12,
        )
    finally:
        await client.aclose()
    # dia1: 10h (1º = manhã) + 18h30 (último = tarde); o gerador inclui manhã.
    assert slots[0].start == _dt(2026, 6, 9, 10, 0)
    assert slots[1].start == _dt(2026, 6, 9, 18, 30)
    assert any(s.start.hour < 12 for s in slots)


@pytest.mark.asyncio
async def test_find_slots_exclui_horarios_ja_oferecidos(respx_mock):
    """G1 (2026-06-16): re-oferta exclui horários já oferecidos — o 14h
    excluído não reaparece; o gerador escolhe o próximo livre (14h30)."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(200, json={"access_token": "t", "expires_in": 3600}),
    )
    respx_mock.post(
        "https://www.googleapis.com/calendar/v3/freeBusy",
    ).mock(return_value=httpx.Response(
        200, json={"calendars": {"primary": {"busy": []}}},
    ))
    now = _dt(2026, 6, 9, 10, 0)
    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        slots = await client.find_available_slots(
            business_hours_start=14, business_hours_end=19,
            slot_min=30, buffer_min=0,
            lookahead_days=5, num_slots=4, now=now,
            exclude_isos={"2026-06-09T14:00:00-03:00"},
        )
    finally:
        await client.aclose()
    isos = {s.start.isoformat() for s in slots}
    assert "2026-06-09T14:00:00-03:00" not in isos   # excluído não volta
    assert slots[0].start == _dt(2026, 6, 9, 14, 30)  # próximo livre do dia


@pytest.mark.asyncio
async def test_list_events_parseia_eventos(respx_mock):
    """D4 (25/jun): list_events extrai id/summary/start/attendees/meet; all-day
    (sem dateTime) vira start_iso None; email normalizado; flags self/organizer."""
    respx_mock.post("https://oauth2.googleapis.com/token").mock(
        return_value=httpx.Response(
            200, json={"access_token": "t", "expires_in": 3600},
        ),
    )
    respx_mock.get(
        "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    ).mock(return_value=httpx.Response(200, json={"items": [
        {
            "id": "evt-1",
            "summary": "Reunião João",
            "start": {"dateTime": "2026-06-26T10:00:00-03:00"},
            "attendees": [
                {"email": "Joao@Exemplo.com", "responseStatus": "accepted"},
                {"email": "mario@noviello.adv.br", "self": True, "organizer": True},
            ],
            "hangoutLink": "https://meet.google.com/abc",
        },
        {"id": "evt-allday", "summary": "Audiência", "start": {"date": "2026-06-26"}},
    ]}))

    client = GoogleCalendarClient(client_id="c", client_secret="s", refresh_token="r")
    try:
        eventos = await client.list_events(
            time_min=datetime.datetime(2026, 6, 25, tzinfo=ZoneInfo("America/Sao_Paulo")),
            time_max=datetime.datetime(2026, 6, 27, tzinfo=ZoneInfo("America/Sao_Paulo")),
        )
    finally:
        await client.aclose()

    assert len(eventos) == 2
    e1 = eventos[0]
    assert e1["id"] == "evt-1"
    assert e1["start_iso"] == "2026-06-26T10:00:00-03:00"
    assert e1["meet_link"] == "https://meet.google.com/abc"
    externos = [a for a in e1["attendees"] if not a["self"] and not a["organizer"]]
    assert externos == [{"email": "joao@exemplo.com", "self": False, "organizer": False}]
    assert eventos[1]["start_iso"] is None  # all-day
