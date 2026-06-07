"""Tests for the outbound HTTP layer."""

import httpx
import pytest

from noviello_funil.outbound import (
    JurichatClient,
    OutboundError,
    format_notification,
    notify_mario,
    with_retry,
)


@pytest.mark.asyncio
async def test_with_retry_succeeds_first_attempt():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        return "ok"

    result = await with_retry(op, attempts=3, base_delay=0.001)
    assert result == "ok"
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_with_retry_succeeds_after_failures():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.HTTPError("boom")
        return "ok"

    result = await with_retry(op, attempts=3, base_delay=0.001)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_with_retry_gives_up_after_max():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise httpx.HTTPError("boom")

    with pytest.raises(OutboundError):
        await with_retry(op, attempts=3, base_delay=0.001)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_jurichat_send_message_calls_correct_endpoint(respx_mock):
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(200, json={"id": "msg-1"}))

    client = JurichatClient(
        api_key="jk-test", base_url="https://api.jurichat.com",
    )
    try:
        result = await client.send_message(
            conversation_id="C-1", text="Olá Maria",
        )
    finally:
        await client.aclose()

    assert route.called
    assert result == {"id": "msg-1"}
    sent_form = route.calls.last.request.read()
    assert b'conversation_id' in sent_form
    assert b'C-1' in sent_form
    assert b"Ol" in sent_form  # accent-encoded


@pytest.mark.asyncio
async def test_jurichat_get_conversation_returns_transcript(respx_mock):
    respx_mock.get(
        "https://api.jurichat.com/conversation/C-1"
    ).mock(return_value=httpx.Response(
        200, json={
            "id": "C-1",
            "transcription": "Lead: oi\nAtendente: ola",
            "summary": "primeiro contato",
        },
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.get_conversation("C-1")
    finally:
        await client.aclose()

    assert result["transcription"].startswith("Lead:")


@pytest.mark.asyncio
async def test_jurichat_get_lead_tags_returns_list(respx_mock):
    respx_mock.get(
        "https://api.jurichat.com/crm/lead/L-1"
    ).mock(return_value=httpx.Response(
        200, json={"id": "L-1", "tags": [
            {"name": "Fazer Follow up"}, {"name": "Proposta enviada"},
        ]},
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        tags = await client.get_lead_tags("L-1")
    finally:
        await client.aclose()

    assert "Fazer Follow up" in tags
    assert "Proposta enviada" in tags


@pytest.mark.asyncio
async def test_jurichat_retries_on_5xx(respx_mock):
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(side_effect=[
        httpx.Response(503),
        httpx.Response(503),
        httpx.Response(200, json={"id": "msg-ok"}),
    ])

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.send_message("C-1", "ola", base_delay=0.001)
    finally:
        await client.aclose()

    assert route.call_count == 3
    assert result["id"] == "msg-ok"


@pytest.mark.asyncio
async def test_jurichat_does_not_retry_on_4xx(respx_mock):
    """401/404/422 etc give the same answer next time — don't waste retries."""
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(401, json={"error": "unauthorized"}))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_message("C-1", "ola", base_delay=0.001)
    finally:
        await client.aclose()

    assert route.call_count == 1  # NOT 3 — no retries on 4xx


@pytest.mark.asyncio
async def test_jurichat_retries_on_429(respx_mock):
    """Rate-limit (429) IS retried — it's transient."""
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(side_effect=[
        httpx.Response(429),
        httpx.Response(200, json={"id": "msg-after-throttle"}),
    ])

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.send_message("C-1", "ola", base_delay=0.001)
    finally:
        await client.aclose()

    assert route.call_count == 2
    assert result["id"] == "msg-after-throttle"


@pytest.mark.asyncio
async def test_jurichat_get_lead_tags_skips_tags_without_name(respx_mock):
    """Defensive: a tag dict missing 'name' is skipped, not a crash."""
    respx_mock.get(
        "https://api.jurichat.com/crm/lead/L-1"
    ).mock(return_value=httpx.Response(
        200, json={"id": "L-1", "tags": [
            {"name": "Fazer Follow up"},
            {"color": "#fff"},  # missing "name" — should be skipped
            {"name": "Proposta enviada"},
        ]},
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        tags = await client.get_lead_tags("L-1")
    finally:
        await client.aclose()

    assert tags == ["Fazer Follow up", "Proposta enviada"]


def test_format_notification_fechar():
    msg = format_notification(
        tipo="fechar",
        nome="Maria",
        telefone="5511999999999",
        ultima_msg="como faço pra contratar?",
        resumo="Plano negou bariátrica",
        conversation_id="C-42",
    )
    assert msg.startswith("🔥")
    assert "Maria" in msg
    assert "5511999999999" in msg
    assert "C-42" in msg


def test_format_notification_handoff():
    msg = format_notification(
        tipo="handoff",
        nome="João",
        telefone="5511888888888",
        ultima_msg="quero falar com humano",
        resumo=None,
        motivo="pediu falar com humano",
        conversation_id="C-99",
    )
    assert msg.startswith("⚠️")
    assert "pediu falar com humano" in msg


def test_format_notification_turnos_excedidos():
    msg = format_notification(
        tipo="turnos",
        nome="Ana",
        telefone="5511777777777",
        ultima_msg="vou pensar",
        resumo=None,
        conversation_id="C-1",
    )
    assert msg.startswith("⏸")
    assert "20" in msg or "turnos" in msg.lower()


@pytest.mark.asyncio
async def test_notify_mario_sends_to_configured_number(respx_mock):
    # Notification = a send_message to the special Mario notification
    # "conversation" — Jurichat treats this as a normal outbound message.
    respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(200, json={"id": "msg-notif"}))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        await notify_mario(
            client,
            mario_conversation_id="C-MARIO",
            mensagem="🔥 teste de notificação",
        )
    finally:
        await client.aclose()
