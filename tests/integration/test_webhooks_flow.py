"""Integration tests for the webhook receiver."""

import hashlib
import hmac
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from noviello_funil.brain import Decisao
from noviello_funil.db import connect, run_migrations
from noviello_funil.state import (
    Estado,
    create_lead_if_absent,
    get_lead_by_conversation,
    transicao,
)
from noviello_funil.webhooks import build_lead_message_processor, register_webhooks


@pytest.fixture
def db_conn():
    conn = connect(":memory:")
    run_migrations(conn)
    return conn


@pytest.fixture
def app():
    """FastAPI app with webhooks registered and in-memory DB."""
    conn = connect(":memory:")
    run_migrations(conn)

    fastapi_app = FastAPI()
    register_webhooks(
        fastapi_app,
        get_db=lambda: conn,
        webhook_secret="whsec-test",
        process_lead_message=lambda payload: None,  # no-op in this test
    )
    return fastapi_app


@pytest.fixture
def client(app):
    return TestClient(app)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_webhook_returns_401_on_invalid_signature(client):
    body = b'{"event":"chat.conversation.updated","id":"e-1"}'
    r = client.post(
        "/webhooks/jurichat",
        content=body,
        headers={
            "X-JuriChat-Signature": "bad",
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 401


def test_webhook_returns_200_on_valid_signature(client):
    body = b'{"event":"chat.conversation.updated","id":"e-1"}'
    sig = _sign("whsec-test", body)
    r = client.post(
        "/webhooks/jurichat",
        content=body,
        headers={"X-JuriChat-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200


def test_webhook_duplicate_event_returns_200_idempotently(client):
    body = b'{"event":"chat.conversation.updated","id":"e-dup"}'
    sig = _sign("whsec-test", body)
    headers = {"X-JuriChat-Signature": sig, "Content-Type": "application/json"}

    r1 = client.post("/webhooks/jurichat", content=body, headers=headers)
    r2 = client.post("/webhooks/jurichat", content=body, headers=headers)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicated") is True


def test_webhook_responds_fast_with_background_processing():
    """The handler must respond before the background task runs."""
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.webhooks import register_webhooks

    called = {"n": 0}

    def spy_processor(payload):
        called["n"] += 1

    conn = connect(":memory:")
    run_migrations(conn)

    spy_app = FastAPI()
    register_webhooks(
        spy_app,
        get_db=lambda: conn,
        webhook_secret="whsec-test",
        process_lead_message=spy_processor,
    )

    body = b'{"event":"chat.conversation.updated","id":"e-fast"}'
    sig = _sign("whsec-test", body)
    with TestClient(spy_app) as c:
        r = c.post(
            "/webhooks/jurichat",
            content=body,
            headers={"X-JuriChat-Signature": sig, "Content-Type": "application/json"},
        )
    assert r.status_code == 200
    # After TestClient context exits, background tasks have completed
    assert called["n"] == 1


# --- Processor (Cenário A) tests -------------------------------------------


def _payload(conversation_id="C-1", lead_id="L-1", from_lead=True, text="oi"):
    """Build a webhook payload matching what Jurichat sends.

    Schema is provisional — adjust when spec §15.6 is validated against
    a real webhook capture. Currently assumes:
      - 'conversation_id' identifies the conversation
      - 'lead_id' identifies the CRM lead
      - 'from_me' is True when the atendente (Mario) sent the message
    """
    return {
        "event": "chat.conversation.updated",
        "id": f"evt-{lead_id}-{text[:3]}",
        "conversation_id": conversation_id,
        "lead_id": lead_id,
        "contact": {"phone": "5511999999999", "name": "Maria"},
        "message": {"text": text, "from_me": not from_lead},
    }


@pytest.mark.asyncio
async def test_processor_creates_lead_and_responds(db_conn):
    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: oi", "summary": "",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "msg-out"})

    async def fake_triagem(**kwargs):
        return Decisao(acao="responder", mensagem="Olá Maria, como posso ajudar?")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="oi"))

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead is not None
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["turnos"] == 1
    fake_jurichat.send_message.assert_awaited_once()
    args, kwargs = fake_jurichat.send_message.call_args
    # send_message(conversation_id, text)
    assert (args[0] if args else kwargs.get("conversation_id")) == "C-1"


@pytest.mark.asyncio
async def test_processor_propor_transitions_to_aguardando_humano(db_conn):
    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "Lead: quanto custa?", "summary": "",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_triagem(**kwargs):
        return Decisao(
            acao="propor",
            mensagem="Nossa proposta é...",
            resumo_caso="Plano negou bariátrica",
        )

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="quanto custa?"))

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    # Two sends: one to lead, one notification to Mario
    assert fake_jurichat.send_message.await_count == 2


@pytest.mark.asyncio
async def test_processor_from_mario_skips_claude(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock()
    fake_jurichat.send_message = AsyncMock()

    triagem_calls = {"n": 0}

    async def fake_triagem(**kwargs):
        triagem_calls["n"] += 1
        return Decisao(acao="responder", mensagem="x")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(from_lead=False, text="vou cuidar daqui"))

    lead = get_lead_by_conversation(db_conn, "C-1")
    assert lead["estado"] == Estado.AGUARDANDO_HUMANO
    assert triagem_calls["n"] == 0
    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_ignores_lead_msg_when_aguardando_humano(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    lead = get_lead_by_conversation(db_conn, "C-1")
    transicao(db_conn, lead["id"], Estado.AGUARDANDO_HUMANO, motivo="setup")

    fake_jurichat = MagicMock()
    fake_jurichat.send_message = AsyncMock()

    async def fake_triagem(**kwargs):
        return Decisao(acao="responder", mensagem="x")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="oi de novo"))

    fake_jurichat.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_max_turnos_triggers_handoff(db_conn):
    create_lead_if_absent(db_conn, "L-1", "C-1", "5511...", "Maria")
    lead = get_lead_by_conversation(db_conn, "C-1")
    # Pre-load turnos to 19; one more message tips over to 20
    db_conn.execute("UPDATE leads SET turnos = 19 WHERE id = ?", (lead["id"],))

    fake_jurichat = MagicMock()
    fake_jurichat.get_conversation = AsyncMock(return_value={
        "transcription": "...",
    })
    fake_jurichat.send_message = AsyncMock(return_value={"id": "x"})

    async def fake_triagem(**kwargs):
        return Decisao(acao="responder", mensagem="continuo")

    processor = build_lead_message_processor(
        get_db=lambda: db_conn,
        jurichat=fake_jurichat,
        mario_conversation_id="C-MARIO",
        triagem_fn=fake_triagem,
        max_turnos=20,
        followup_horas=48,
    )

    await processor(_payload(text="msg 20"))

    after = get_lead_by_conversation(db_conn, "C-1")
    assert after["estado"] == Estado.AGUARDANDO_HUMANO
    # Notification to Mario, but NO reply sent to lead at turn cap
    # (one send_message call for the Mario notification)
    assert fake_jurichat.send_message.await_count == 1
