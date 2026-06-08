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

from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.outbound import (
    JurichatClient,
    format_notification,
    notify_mario,
)
from noviello_funil.state import (
    ESTADOS_ATIVOS_CLAUDE,
    Estado,
    bump_turnos,
    create_lead_if_absent,
    get_lead_by_conversation,
    is_webhook_processed,
    mark_webhook_processed,
    record_lead_message_received,
    register_error,
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


def _is_from_lead(payload: dict[str, Any]) -> bool:
    """Detect if the message in the payload came FROM the lead (not Mario).

    Currently assumes Jurichat sets `message.from_me = True` for outbound
    (atendente-sent) messages. Adjust here if the real payload differs
    (see spec §15.6).
    """
    msg = payload.get("message") or {}
    return not msg.get("from_me", False)


def _extract_text(payload: dict[str, Any]) -> str:
    return (payload.get("message") or {}).get("text", "")


def build_lead_message_processor(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    mario_conversation_id: str,
    triagem_fn: Callable[..., Any],
    max_turnos: int,
    followup_horas: int,
) -> Callable[[dict[str, Any]], Any]:
    """Build the async processor that handles a single webhook payload.

    Injected dependencies make this testable without real Anthropic/Jurichat.
    `triagem_fn` is the (awaitable) Claude triage callable.
    """

    async def process(payload: dict[str, Any]) -> None:
        conn = get_db()
        conversation_id = payload.get("conversation_id")
        lead_id_external = payload.get("lead_id")
        contact = payload.get("contact") or {}
        ultima_msg = _extract_text(payload)

        if not conversation_id or not lead_id_external:
            # LGPD: log only the keys, never the payload values (may contain
            # lead message body).
            logger.warning("payload missing ids: keys=%s", list(payload.keys()))
            return

        # Branch 1: message from Mario → halt Claude permanently for this lead
        if not _is_from_lead(payload):
            lead = get_lead_by_conversation(conn, conversation_id)
            if lead is None:
                lead = create_lead_if_absent(
                    conn, lead_id_external, conversation_id,
                    contact.get("phone", ""), contact.get("name"),
                )
            if lead["estado"] != Estado.AGUARDANDO_HUMANO:
                transicao(
                    conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                    motivo="mensagem_mario",
                )
            return

        # Branch 2: message from lead
        lead = create_lead_if_absent(
            conn, lead_id_external, conversation_id,
            contact.get("phone", ""), contact.get("name"),
        )
        estado_atual = lead["estado"]

        if estado_atual == Estado.AGUARDANDO_HUMANO:
            # Claude is silent on this lead
            return

        if estado_atual not in ESTADOS_ATIVOS_CLAUDE:
            logger.warning(
                "lead %s in unexpected state %s; skipping",
                lead["id"], estado_atual,
            )
            return

        # Reopen from encerrado_sem_resposta if applicable
        if estado_atual == Estado.ENCERRADO_SEM_RESPOSTA:
            transicao(
                conn, lead["id"], Estado.EM_CONVERSA, motivo="lead_retornou",
            )
            record_lead_message_received(
                conn, lead["id"],
                proxima_acao_horas=followup_horas,
                reset_turnos=True,
            )
        else:
            record_lead_message_received(
                conn, lead["id"], proxima_acao_horas=followup_horas,
            )

        bump_turnos(conn, lead["id"])
        lead = get_lead_by_conversation(conn, conversation_id)
        if lead is None:
            # Should be impossible (we just created/loaded it above), but
            # don't rely on `assert` which is stripped under python -O.
            raise RuntimeError(
                f"lead disappeared after bump_turnos: conversation_id={conversation_id}"
            )

        # Turn cap → force handoff before calling Claude (saves a token)
        if lead["turnos"] >= max_turnos:
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="max_turnos",
            )
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="turnos",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    conversation_id=conversation_id,
                ),
            )
            return

        # Pull transcript and call Claude
        try:
            conv = await jurichat.get_conversation(conversation_id)
            transcript = conv.get("transcription", "")
        except Exception as exc:
            register_error(conn, lead["id"], "jurichat_get_conversation_failed")
            logger.exception("get_conversation failed: %s", exc)
            return

        try:
            decisao: Decisao = await triagem_fn(
                conversation_transcript=transcript,
            )
        except DecisaoInvalida:
            register_error(conn, lead["id"], "claude_invalid_json")
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="claude_erro",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    conversation_id=conversation_id,
                ),
            )
            return

        # Route by acao
        if decisao.acao == "responder":
            await jurichat.send_message(conversation_id, decisao.mensagem)
            return

        if decisao.acao == "propor":
            await jurichat.send_message(conversation_id, decisao.mensagem)
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="claude_propor",
                payload={"resumo_caso": decisao.resumo_caso},
            )
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="fechar",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    resumo=decisao.resumo_caso,
                    conversation_id=conversation_id,
                ),
            )
            return

        if decisao.acao == "handoff":
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="claude_handoff",
                payload={"motivo_handoff": decisao.motivo_handoff},
            )
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=format_notification(
                    tipo="handoff",
                    nome=lead["contato_nome"],
                    telefone=lead["contato_telefone"],
                    ultima_msg=ultima_msg,
                    motivo=decisao.motivo_handoff,
                    conversation_id=conversation_id,
                ),
            )

    return process
