"""Integration tests for the webhook receiver.

After the polling refactor (R01), the webhook processor is just a thin
"register-and-wake": it creates/updates the lead row and bumps the
scheduler's next-action timestamp. All Claude logic lives in
``scheduler.run_poll_cycle`` (tested separately).
"""

import datetime
import hashlib
import hmac

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

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
        process_lead_message=lambda payload: None,  # no-op for HMAC/dedupe tests
    )
    return fastapi_app


@pytest.fixture
def client(app):
    return TestClient(app)


def _sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# --- HMAC + idempotency tests (transport layer) ----------------------------


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


def test_webhook_accepts_sha256_prefixed_signature(client):
    """Jurichat sends X-JuriChat-Signature as 'sha256=<hex>' (real format
    captured 2026-06-07). The verifier must strip the prefix."""
    body = b'{"event":"webhook.test"}'
    sig = "sha256=" + _sign("whsec-test", body)
    r = client.post(
        "/webhooks/jurichat",
        content=body,
        headers={"X-JuriChat-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200


def test_webhook_uses_jurichat_delivery_header_for_idempotency(client):
    """When X-JuriChat-Delivery is present, it should be used as event_id
    (instead of payload['id'] or hash). Sending the same delivery id twice
    must dedupe even if the body differs slightly."""
    body1 = b'{"event":"webhook.test","timestamp":"2026-06-07T01:00:00Z"}'
    body2 = b'{"event":"webhook.test","timestamp":"2026-06-07T01:00:01Z"}'
    sig1 = "sha256=" + _sign("whsec-test", body1)
    sig2 = "sha256=" + _sign("whsec-test", body2)
    delivery = "cmq4kdfcs00ugo10idkzycrn2"

    r1 = client.post(
        "/webhooks/jurichat",
        content=body1,
        headers={
            "X-JuriChat-Signature": sig1,
            "X-JuriChat-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )
    r2 = client.post(
        "/webhooks/jurichat",
        content=body2,
        headers={
            "X-JuriChat-Signature": sig2,
            "X-JuriChat-Delivery": delivery,
            "Content-Type": "application/json",
        },
    )
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json().get("duplicated") is True


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


# --- Register-and-wake processor tests -------------------------------------


def _payload(
    *,
    conversation_id="cmq4ljg3t872iqn07n969t7df",
    person_id="cmntckrc40866qt0i9ih9al1q",
    person_name="Mario eu",
    person_phone="5511992046888",
    status="ROBOT_INTERACTIVE",
    previous_status="INACTIVE",
    event="chat.conversation.updated",
):
    """Build a Jurichat webhook payload matching the real shape captured
    from a test event on 2026-06-07.
    """
    return {
        "event": event,
        "data": {
            "status": status,
            "previousStatus": previous_status,
            "inboxId": "cmhphehs612ucpp0ilvlf0cv9v",
            "personId": person_id,
            "inboxName": "Canal Inicial",
            "personName": person_name,
            "personPhone": person_phone,
            "conversationId": conversation_id,
        },
        "timestamp": "2026-06-08T02:33:36.201Z",
    }


@pytest.mark.asyncio
async def test_processor_creates_lead_with_immediate_poll(db_conn):
    """A brand-new conversation webhook creates the lead in em_conversa
    AND schedules an immediate poll (proxima_acao_em <= now)."""
    processor = build_lead_message_processor(get_db=lambda: db_conn)

    await processor(_payload())

    lead = get_lead_by_conversation(db_conn, "cmq4ljg3t872iqn07n969t7df")
    assert lead is not None
    assert lead["estado"] == Estado.EM_CONVERSA
    assert lead["turnos"] == 0  # turn counting is the scheduler's job now
    assert lead["contato_nome"] == "Mario eu"
    assert lead["contato_telefone"] == "5511992046888"
    # proxima_acao_em should be set (poll request)
    assert lead["proxima_acao_em"] is not None
    # And it should be "now or in the past" so the next tick picks it up
    proxima = datetime.datetime.strptime(
        lead["proxima_acao_em"], "%Y-%m-%d %H:%M:%S"
    )
    assert proxima <= datetime.datetime.utcnow() + datetime.timedelta(seconds=5)


@pytest.mark.asyncio
async def test_processor_is_idempotent_on_repeated_webhook(db_conn):
    """Same person_id arriving twice doesn't create two leads — just
    bumps the existing one's schedule."""
    processor = build_lead_message_processor(get_db=lambda: db_conn)

    await processor(_payload())
    await processor(_payload())

    rows = db_conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()
    assert rows["n"] == 1


@pytest.mark.asyncio
async def test_processor_skips_lead_in_aguardando_humano(db_conn):
    """If Mario already took the lead, the webhook must not re-wake it."""
    create_lead_if_absent(
        db_conn,
        jurichat_lead_id="cmntckrc40866qt0i9ih9al1q",
        jurichat_conversation_id="cmq4ljg3t872iqn07n969t7df",
        contato_telefone="5511992046888",
        contato_nome="Mario eu",
    )
    lead = get_lead_by_conversation(db_conn, "cmq4ljg3t872iqn07n969t7df")
    transicao(db_conn, lead["id"], Estado.AGUARDANDO_HUMANO, motivo="setup")

    processor = build_lead_message_processor(get_db=lambda: db_conn)
    await processor(_payload())

    after = get_lead_by_conversation(db_conn, "cmq4ljg3t872iqn07n969t7df")
    assert after["estado"] == Estado.AGUARDANDO_HUMANO  # unchanged
    assert after["proxima_acao_em"] is None  # NOT re-scheduled for polling


@pytest.mark.asyncio
async def test_processor_reopens_lead_from_encerrado(db_conn):
    """A lead that was closed (encerrado_sem_resposta) reopens on the
    next webhook — back to em_conversa + scheduled for polling."""
    create_lead_if_absent(
        db_conn,
        jurichat_lead_id="cmntckrc40866qt0i9ih9al1q",
        jurichat_conversation_id="cmq4ljg3t872iqn07n969t7df",
        contato_telefone="5511992046888",
        contato_nome="Mario eu",
    )
    lead = get_lead_by_conversation(db_conn, "cmq4ljg3t872iqn07n969t7df")
    transicao(db_conn, lead["id"], Estado.ENCERRADO_SEM_RESPOSTA, motivo="timer")

    processor = build_lead_message_processor(get_db=lambda: db_conn)
    await processor(_payload())

    after = get_lead_by_conversation(db_conn, "cmq4ljg3t872iqn07n969t7df")
    assert after["estado"] == Estado.EM_CONVERSA
    assert after["proxima_acao_em"] is not None


@pytest.mark.asyncio
async def test_processor_ignores_payload_without_ids(db_conn):
    """Malformed payloads (missing conversationId/personId) log a warning
    and exit cleanly — no crash, no lead created."""
    processor = build_lead_message_processor(get_db=lambda: db_conn)

    await processor({"event": "chat.conversation.updated", "data": {}})

    rows = db_conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()
    assert rows["n"] == 0
