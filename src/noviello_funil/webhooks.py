"""HTTP entry point: receives Jurichat webhooks.

Responsibilities:
- HMAC-SHA256 signature verification (header X-JuriChat-Signature)
- Idempotency check via webhooks_recebidos table
- 200 response in <100ms — heavy work runs in BackgroundTask
- Delegates actual processing to the injected `process_lead_message`
  callable (defined in main.py, wires up state + brain + outbound)
"""

import hashlib
import hmac
import logging
from collections.abc import Callable
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response

from noviello_funil.state import (
    Estado,
    create_lead_if_absent,
    is_webhook_processed,
    mark_webhook_processed,
    schedule_next_action,
    transicao,
)

logger = logging.getLogger(__name__)


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification.

    Jurichat sends the signature as ``sha256=<hex>`` (GitHub-style),
    confirmed by inspecting a real webhook test event on 2026-06-07.
    We strip the prefix before comparing.

    We accept raw hex too (no prefix) as a forward-compat safety net in
    case Jurichat ever changes the format back.
    """
    if not signature:
        return False
    if signature.startswith("sha256="):
        signature = signature[len("sha256=") :]
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_event_id(
    payload: dict[str, Any], headers: dict[str, str] | None = None,
) -> str:
    """Best-effort event id for idempotency.

    Order of preference (most reliable first):
      1. ``X-JuriChat-Delivery`` header — Jurichat's own unique ID per
         delivery attempt. Confirmed real on 2026-06-07 inspection.
      2. ``payload["id"]`` or ``payload["event_id"]`` — fallback.
      3. Hash of the body — last resort.
    """
    if headers:
        # Headers in FastAPI are case-insensitive on access via
        # request.headers, but when a dict is passed in for testing
        # we accept both cases.
        delivery = headers.get("x-jurichat-delivery") or headers.get(
            "X-JuriChat-Delivery"
        )
        if delivery:
            return delivery
    return (
        payload.get("id")
        or payload.get("event_id")
        or hashlib.sha256(repr(payload).encode()).hexdigest()[:16]
    )


def register_webhooks(
    app: FastAPI,
    *,
    get_db: Callable[[], Any],
    webhook_secret: str,
    process_lead_message: Callable[[dict[str, Any]], Any],
) -> None:
    """Register POST /webhooks/jurichat on `app`."""

    @app.post("/webhooks/jurichat")
    async def jurichat_webhook(
        request: Request, background_tasks: BackgroundTasks,
    ) -> Response:
        body = await request.body()
        signature = request.headers.get("X-JuriChat-Signature")

        if not _verify_signature(webhook_secret, body, signature):
            logger.warning("webhook hmac invalid (signature=%r)", signature)
            raise HTTPException(status_code=401, detail="invalid signature")

        try:
            payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"bad json: {exc}") from exc

        conn = get_db()
        event_id = _extract_event_id(payload, headers=dict(request.headers))
        if is_webhook_processed(conn, "jurichat", event_id):
            logger.info("webhook duplicate event_id=%s", event_id)
            return Response(
                content=b'{"ok":true,"duplicated":true}',
                media_type="application/json",
            )

        hash_payload = hashlib.sha256(body).hexdigest()
        mark_webhook_processed(conn, "jurichat", event_id, hash_payload)

        background_tasks.add_task(process_lead_message, payload)

        return Response(
            content=b'{"ok":true}', media_type="application/json",
        )


# Jurichat status values seen in the wild (captured 2026-06-07).
# ROBOT_INTERACTIVE means the conversation was assigned to a bot agent —
# our cue to start polling for new lead messages.
_STATUSES_TRIGGER_POLL = frozenset({
    "ROBOT_INTERACTIVE",
    "ACTIVE",
    "WAITING",
})


def _extract_lead_fields(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Pull lead identifiers from a Jurichat webhook payload.

    Real payload shape captured 2026-06-07 from `chat.conversation.updated`:
        {
          "event": "chat.conversation.updated",
          "data": {
            "conversationId": "...",
            "personId": "...",
            "personName": "...",
            "personPhone": "...",
            "status": "ROBOT_INTERACTIVE",
            "previousStatus": "INACTIVE",
            ...
          },
          "timestamp": "..."
        }

    Returns None when required ids are missing.
    """
    data = payload.get("data") or {}
    conversation_id = data.get("conversationId")
    person_id = data.get("personId")
    if not conversation_id or not person_id:
        return None
    return {
        "conversation_id": conversation_id,
        "person_id": person_id,
        "person_name": data.get("personName"),
        "person_phone": data.get("personPhone", ""),
        "status": data.get("status"),
        "previous_status": data.get("previousStatus"),
    }


def build_lead_message_processor(
    *,
    get_db: Callable[[], Any],
) -> Callable[[dict[str, Any]], Any]:
    """Register-and-wake processor for Jurichat webhooks.

    Jurichat has NO per-message webhook event — `chat.conversation.updated`
    fires only on status changes (assignment to bot, closure, etc.).
    Confirmed by inspecting real webhook traffic 2026-06-07.

    So this processor does NOT call Claude. It only:
      1. Maps the payload to a stable lead record (creates if new).
      2. Marks the lead's ``proxima_acao_em = now`` so the next scheduler
         tick picks it up and pulls the actual transcript via the API.

    All Claude logic lives in ``scheduler.run_poll_cycle`` (poll-based).
    """

    async def process(payload: dict[str, Any]) -> None:
        conn = get_db()
        event = payload.get("event", "")
        fields = _extract_lead_fields(payload)

        if fields is None:
            logger.warning(
                "webhook payload missing required ids: event=%s keys=%s",
                event, list((payload.get("data") or {}).keys()),
            )
            return

        # Idempotent lead registration (uses person_id as the stable CRM id)
        lead = create_lead_if_absent(
            conn,
            jurichat_lead_id=fields["person_id"],
            jurichat_conversation_id=fields["conversation_id"],
            contato_telefone=fields["person_phone"],
            contato_nome=fields["person_name"],
        )

        # Reopen if the lead was previously closed and a new conversation
        # event came in (lead returned).
        if lead["estado"] == Estado.ENCERRADO_SEM_RESPOSTA:
            transicao(
                conn, lead["id"], Estado.EM_CONVERSA,
                motivo="webhook_lead_retornou",
                payload={"event": event, "status": fields["status"]},
            )

        # If lead is already terminal-for-bot, ignore (Mario assumed it).
        if lead["estado"] == Estado.AGUARDANDO_HUMANO:
            return

        # Wake the poller: set proxima_acao_em = now (1s ahead to avoid
        # racing the current tick).
        # Using horas=0 with a positive seconds offset isn't supported by
        # schedule_next_action; using 0 hours is effectively "immediate".
        schedule_next_action(conn, lead["id"], horas=0)

    return process
