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
async def test_find_slots_returns_first_n_when_calendar_empty(respx_mock):
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
            lookahead_days=5, num_slots=3, now=now,
        )
    finally:
        await client.aclose()

    # Calendar vazio + agora 10h → primeiros 3 slots de hoje: 14h, 14h30, 15h
    assert len(slots) == 3
    assert slots[0].start == _dt(2026, 6, 9, 14, 0)
    assert slots[1].start == _dt(2026, 6, 9, 14, 30)
    assert slots[2].start == _dt(2026, 6, 9, 15, 0)


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
