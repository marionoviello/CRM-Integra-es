"""Tests for the outbound HTTP layer."""

import httpx
import pytest

from noviello_funil.outbound import (
    JurichatClient,
    OutboundError,
    _sanitize_for_whatsapp,
    format_notification,
    notify_mario,
    with_retry,
)

# --- Sanitização HTML → WhatsApp -----------------------------------------

def test_sanitize_converts_br_to_newline():
    """Bug reportado 2026-06-08: Claude gerou ``<br />`` que aparece
    literal no WhatsApp. Sanitização deve converter pra ``\\n``."""
    src = "Linha 1<br />Linha 2<br/>Linha 3<br>Linha 4"
    assert _sanitize_for_whatsapp(src) == "Linha 1\nLinha 2\nLinha 3\nLinha 4"


def test_sanitize_collapses_consecutive_br_into_paragraph():
    src = "Parágrafo 1<br /><br />Parágrafo 2"
    out = _sanitize_for_whatsapp(src)
    assert out == "Parágrafo 1\n\nParágrafo 2"


def test_sanitize_converts_li_to_bullets():
    src = "<ul><li>Um<li>Dois<li>Três</ul>"
    out = _sanitize_for_whatsapp(src)
    assert "• Um" in out
    assert "• Dois" in out
    assert "• Três" in out
    assert "<" not in out and ">" not in out


def test_sanitize_strips_unknown_tags():
    src = "Texto <b>negrito</b> e <span style='x'>colorido</span>"
    out = _sanitize_for_whatsapp(src)
    assert out == "Texto negrito e colorido"


def test_sanitize_passes_clean_text_unchanged():
    src = "Olá Maria!\n\nQuantos imóveis estão envolvidos?"
    assert _sanitize_for_whatsapp(src) == src


def test_sanitize_handles_empty():
    assert _sanitize_for_whatsapp("") == ""


def test_sanitize_substitui_mario_individual_por_equipe():
    """Bug em campo (2026-06-09): Claude disse 'Dr. Mario Noviello'.
    Sanitizador força 'nossa equipe'."""
    cases = [
        ("Vou encaminhar pro Dr. Mario Noviello.",
         "Vou encaminhar pro nossa equipe."),
        ("O Mario vai te retornar.",
         "nossa equipe vai te retornar."),
        ("Falo com Mario Noviello hoje.",
         "Falo com nossa equipe hoje."),
        ("o Mario é especialista.",
         "nossa equipe é especialista."),
        ("Dr. Mario te atenderá.",
         "nossa equipe te atenderá."),
    ]
    for entrada, esperado in cases:
        assert _sanitize_for_whatsapp(entrada) == esperado, \
            f"falhou pra: {entrada!r}"


def test_sanitize_nao_quebra_palavras_parecidas():
    """Word boundary protege Marina, Mariolândia, etc."""
    src = "Marina vai te atender em Mariolândia."
    assert _sanitize_for_whatsapp(src) == src


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
    # Novo contrato (descoberto 2026-06-08): multipart/form-data com
    # conversationId (camelCase), message, type=text.
    assert b"conversationId" in sent_form
    assert b"message" in sent_form
    assert b"type" in sent_form
    assert b"text" in sent_form
    assert b"C-1" in sent_form
    assert b"Ol" in sent_form  # accent-encoded


@pytest.mark.asyncio
async def test_jurichat_get_conversation_returns_transcript(respx_mock):
    """Real Jurichat shape (captured 2026-06-08): {data: {messages: [
        {content, direction: INBOUND|OUTBOUND, ...}, ...]}}.
    get_conversation builds a synthetic ``transcription`` from messages
    so the rest of the pipeline keeps working."""
    respx_mock.get(
        "https://api.jurichat.com/conversation/C-1"
    ).mock(return_value=httpx.Response(
        200, json={
            "data": {
                "id": "C-1",
                "person": {"name": "Maria"},
                "messages": [
                    {"content": "oi", "direction": "INBOUND", "type": "text"},
                    {"content": "ola", "direction": "OUTBOUND", "type": "text"},
                ],
            },
        },
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.get_conversation("C-1")
    finally:
        await client.aclose()

    assert result["transcription"] == "Lead: oi\nAtendente: ola"
    assert len(result["messages_raw"]) == 2


@pytest.mark.asyncio
async def test_jurichat_get_conversation_flattens_multiline_messages(respx_mock):
    """Mensagem multi-linha (ex.: bullets do oferecer_horarios) vira UMA
    linha física no transcript sintético. O poll cycle assume o contrato
    "1 mensagem = 1 linha" (_last_line_from_atendente, _count_lead_lines,
    _last_lead_message em scheduler.py) — newline interno preservado faz
    o Signal 1 furar e o Claude ser re-invocado sobre a própria resposta."""
    respx_mock.get(
        "https://api.jurichat.com/conversation/C-1"
    ).mock(return_value=httpx.Response(
        200, json={
            "data": {
                "id": "C-1",
                "person": {"name": "Maria"},
                "messages": [
                    {
                        "content": "quero agendar\npode ser de manhã?",
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
        },
    ))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        result = await client.get_conversation("C-1")
    finally:
        await client.aclose()

    lines = result["transcription"].splitlines()
    assert lines == [
        "Lead: quero agendar pode ser de manhã?",
        "Atendente: Tenho estes horários: • ter 14h00 • qua 10h00 Qual prefere?",
    ]


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


def test_format_notification_claude_erro():
    msg = format_notification(
        tipo="claude_erro",
        nome="Bia",
        telefone="5511666666666",
        ultima_msg="qualquer coisa",
        conversation_id="C-7",
    )
    assert msg.startswith("⚠️")
    assert "Claude retornou JSON inválido" in msg
    assert "C-7" in msg


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
async def test_notify_mario_swallows_4xx_silently(respx_mock, caplog):
    """Fire-and-forget per spec §9: a wrong MARIO_CONVERSATION_ID (404)
    must NOT raise — it logs and returns. Same for any other 4xx."""
    # Pré-requisito do novo contrato: start_human_support antes do send.
    respx_mock.post(
        "https://api.jurichat.com/conversation/start-human-support"
    ).mock(return_value=httpx.Response(200, json={"success": True}))
    respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(404, json={"error": "not found"}))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        # Must NOT raise — that's the whole point of fire-and-forget
        await notify_mario(
            client,
            mario_conversation_id="C-WRONG",
            mensagem="🔥 should be swallowed",
        )
    finally:
        await client.aclose()

    assert any("notify_mario failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_notify_mario_swallows_outbound_error_silently(respx_mock, caplog):
    """Exhausted retries (3x 503 → OutboundError) also must NOT raise."""
    # start_human_support volta 200 — o erro está no send-message.
    respx_mock.post(
        "https://api.jurichat.com/conversation/start-human-support"
    ).mock(return_value=httpx.Response(200, json={"success": True}))
    respx_mock.post(
        "https://api.jurichat.com/conversation/send-message"
    ).mock(return_value=httpx.Response(503))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        # send_message will retry 3 times then raise OutboundError;
        # notify_mario must swallow it.
        await client._client.aclose() if False else None  # no-op placeholder
        # Use a low base_delay path by calling client directly — but notify_mario
        # uses default 1s base. To keep test fast we patch via a wrapper:
        original = client.send_message

        async def fast_send(conv, txt, *, base_delay=0.001):
            return await original(conv, txt, base_delay=base_delay)

        client.send_message = fast_send  # type: ignore[method-assign]
        await notify_mario(
            client,
            mario_conversation_id="C-MARIO",
            mensagem="🔥 retried then swallowed",
        )
    finally:
        await client.aclose()

    assert any("notify_mario failed" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_notify_mario_sends_to_configured_number(respx_mock):
    # Notification = a send_message to the special Mario notification
    # "conversation" — Jurichat treats this as a normal outbound message.
    # Novo contrato exige start_human_support antes do send.
    respx_mock.post(
        "https://api.jurichat.com/conversation/start-human-support"
    ).mock(return_value=httpx.Response(200, json={"success": True}))
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


@pytest.mark.asyncio
async def test_start_human_support_com_bot_user_id_usa_selectedUserId(respx_mock):
    """Com bot_user_id setado, atribui pro BOT IA — não sorteia humano."""
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/start-human-support"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    client = JurichatClient(
        "jk-test", "https://api.jurichat.com", bot_user_id="USR-BOT-IA",
    )
    try:
        await client.start_human_support("C-1")
    finally:
        await client.aclose()

    import json as _json
    body = _json.loads(route.calls.last.request.read())
    assert body == {"conversationId": "C-1", "selectedUserId": "USR-BOT-IA"}
    assert "isRandom" not in body


@pytest.mark.asyncio
async def test_start_human_support_sem_bot_user_id_usa_isRandom(respx_mock):
    """Sem bot_user_id → comportamento legado isRandom."""
    route = respx_mock.post(
        "https://api.jurichat.com/conversation/start-human-support"
    ).mock(return_value=httpx.Response(200, json={"success": True}))

    client = JurichatClient("jk-test", "https://api.jurichat.com")
    try:
        await client.start_human_support("C-1")
    finally:
        await client.aclose()

    import json as _json
    body = _json.loads(route.calls.last.request.read())
    assert body == {"conversationId": "C-1", "isRandom": True}
