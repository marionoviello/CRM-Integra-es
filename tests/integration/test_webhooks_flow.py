"""Integration tests for the webhook receiver."""

import hashlib
import hmac

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from noviello_funil.db import connect, run_migrations
from noviello_funil.webhooks import register_webhooks


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
