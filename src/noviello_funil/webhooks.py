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

from noviello_funil.state import is_webhook_processed, mark_webhook_processed

logger = logging.getLogger(__name__)


def _verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    """Constant-time HMAC-SHA256 verification.

    Assumes Jurichat sends the raw hex digest (no ``sha256=`` prefix).
    If they ever switch to GitHub-style ``sha256=<hex>``, strip the
    prefix here before comparing.
    """
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _extract_event_id(payload: dict[str, Any]) -> str:
    """Best-effort event id. Falls back to a payload hash."""
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
        event_id = _extract_event_id(payload)
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
