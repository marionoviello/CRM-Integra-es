"""HTTP client for outbound calls to Jurichat and to send notifications to Mario.

Uses httpx.AsyncClient. All operations go through `with_retry` for
transient-failure resilience.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

logger = logging.getLogger(__name__)

T = TypeVar("T")


class OutboundError(Exception):
    """Raised when an outbound call exhausts all retries."""


_RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


async def with_retry(
    op: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
) -> T:
    """Run async `op` with exponential backoff on transient failures only.

    Delays: ``base_delay * 3 ** (attempt - 1)`` — so 1s, 3s, 9s for default.
    Raises ``OutboundError`` if all attempts fail (preserves last error in
    ``__cause__``).

    Retried:
        - Network/transport errors (``httpx.TransportError``)
        - Timeouts (``httpx.TimeoutException``)
        - HTTP 408/429/500/502/503/504 (``httpx.HTTPStatusError``)
        - Bare ``httpx.HTTPError`` (kept for cases where callers raise
          the generic class — uncommon, mostly in tests)

    NOT retried (raised immediately to bubble up to the caller):
        - HTTP 4xx other than 408/429 — they will give the same answer next
          time and retrying just burns latency and log noise.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await op()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRYABLE_STATUS_CODES:
                raise
            last_exc = exc
        except (httpx.TransportError, httpx.TimeoutException) as exc:
            last_exc = exc
        except httpx.HTTPError as exc:
            # Fallback: generic HTTPError (subclass not matched above). Real
            # httpx code rarely raises this directly, but tests and exotic
            # transports might.
            last_exc = exc

        if attempt == attempts:
            break
        delay = base_delay * (3 ** (attempt - 1))
        logger.warning(
            "outbound_retry attempt=%d/%d delay=%.1fs err=%s",
            attempt, attempts, delay, last_exc,
        )
        await asyncio.sleep(delay)
    raise OutboundError(f"all {attempts} attempts failed") from last_exc


class JurichatClient:
    """Thin wrapper over Jurichat REST API."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"x-jurichat-api-key": api_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def start_human_support(
        self,
        conversation_id: str,
        *,
        is_random: bool = True,
        base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /conversation/start-human-support (application/json).

        Transfere a conversa para "modo atendimento humano" — pré-requisito
        para poder enviar mensagens via send-message. Sem isso, send-message
        retorna 400 "Conversa não está em modo de atendimento humano".

        Confirmado idempotente: chamar várias vezes na mesma conversa
        retorna sempre {"success": true} sem efeito colateral.

        ``is_random=True`` deixa o Jurichat escolher qualquer atendente
        (não precisamos especificar selectedUserId).

        Confirmado 2026-06-08 via curl: STATUS 200 + {"success": true}.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/conversation/start-human-support",
                json={"conversationId": conversation_id, "isRandom": is_random},
            )
            if resp.status_code >= 400:
                logger.error(
                    "start_human_support status=%d body=%r",
                    resp.status_code, resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        *,
        base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /conversation/send-message (multipart/form-data).

        Formato real descoberto 2026-06-08:
          - Content-Type: multipart/form-data (não JSON, não urlencoded)
          - conversationId: camelCase (não snake_case)
          - message: campo para o texto (não 'text')
          - type: "text" para mensagens textuais (obrigatório)

        Pré-requisito: conversa em modo human-support. Veja
        ``start_human_support``.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/conversation/send-message",
                files={
                    "conversationId": (None, conversation_id),
                    "message": (None, text),
                    "type": (None, "text"),
                },
            )
            if resp.status_code >= 400:
                logger.error(
                    "send_message status=%d body=%r",
                    resp.status_code, resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def get_conversation(
        self, conversation_id: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """GET /conversation/{id} — returns full conversation with messages.

        Response real captured 2026-06-08:
            { "data": { "person": {...}, "messages": [
                { "content": "...", "direction": "INBOUND"|"OUTBOUND",
                  "messageAt": "...", "type": "text", ... },
                ...
            ]}}

        We build a synthetic ``transcription`` string from the messages
        array for backward compat with the rest of the pipeline (poll
        cycle does ``conv.get("transcription", "")``).

        ``direction = INBOUND`` → lead enviou.
        ``direction = OUTBOUND`` → atendente (humano ou bot) enviou.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/conversation/{conversation_id}"
            )
            resp.raise_for_status()
            return resp.json()

        raw = await with_retry(op, attempts=3, base_delay=base_delay)
        data = raw.get("data") or raw  # API às vezes encapsula em "data"
        messages = data.get("messages") or []

        # Constroi transcript no formato esperado pelo poll cycle:
        # "Lead: <texto>\nAtendente: <texto>\n..."
        lines: list[str] = []
        for msg in messages:
            content = (msg.get("content") or "").strip()
            if not content:
                continue
            direction = msg.get("direction", "")
            prefix = "Lead:" if direction == "INBOUND" else "Atendente:"
            lines.append(f"{prefix} {content}")

        # Resposta enriquecida: mantém os dados originais + transcription
        # sintética que o resto do código já consome.
        return {
            **data,
            "transcription": "\n".join(lines),
            "messages_raw": messages,
        }

    async def list_active_conversations(
        self,
        *,
        inbox_id: str,
        page: int = 1,
        limit: int = 100,
        base_delay: float = 1.0,
    ) -> list[dict[str, Any]]:
        """GET /conversation — lista conversas de uma inbox.

        Parâmetros confirmados via 400 Validation Error 2026-06-08:
          - ``inboxId``: obrigatório (sem ele 400).
          - ``page``, ``limit``: strings (int causa 400).

        Response shape (per item):
            {
                "id": "<conversation_id>",
                "isArchived": false,
                "isGroup": false,
                "person": { "id": "...", "name": "...", "phoneNumber": "..." },
                ...
            }

        Used by the scheduler to sync Jurichat → our DB. Since Jurichat
        has NO per-message webhook event, we poll the conversation list
        periodically to discover new leads.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/conversation",
                params={
                    "inboxId": inbox_id,
                    "page": str(page),
                    "limit": str(limit),
                },
            )
            if resp.status_code >= 400:
                # Log do body do erro pra debug rápido — 400/422 do Jurichat
                # geralmente vem com mensagem específica do parâmetro errado.
                logger.error(
                    "list_active_conversations status=%d body=%r",
                    resp.status_code, resp.text[:500],
                )
            resp.raise_for_status()
            return resp.json()

        try:
            data = await with_retry(op, attempts=3, base_delay=base_delay)
        except httpx.HTTPStatusError as exc:
            # Wrapping em OutboundError pra o scheduler tratar uniformemente
            # (sem propagar o httpx pra cima).
            raise OutboundError(
                f"list_active_conversations falhou {exc.response.status_code}"
            ) from exc
        return data.get("data", [])

    async def get_lead_tags(
        self, lead_id: str, *, base_delay: float = 1.0,
    ) -> list[str]:
        """GET /crm/lead/{id} — returns list of tag names (empty if none).

        NOTE: exact endpoint shape pending confirmation (see spec §15.4).
        Adjust the path if Jurichat docs differ.
        """

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/crm/lead/{lead_id}"
            )
            resp.raise_for_status()
            return resp.json()

        data = await with_retry(op, attempts=3, base_delay=base_delay)
        # Defensive: skip tag dicts missing a "name" key rather than crash.
        return [t["name"] for t in data.get("tags", []) if "name" in t]


def format_notification(
    *,
    tipo: str,
    nome: str | None,
    telefone: str,
    ultima_msg: str,
    resumo: str | None = None,
    motivo: str | None = None,
    conversation_id: str,
) -> str:
    """Format a notification message for Mario.

    tipo: 'fechar' | 'handoff' | 'turnos' | 'claude_erro'
    """
    nome_label = nome or "(sem nome)"

    if tipo == "fechar":
        head = f"🔥 Lead {nome_label} ({telefone}) — QUER FECHAR"
        body = f'Última msg: "{ultima_msg}"'
        extra = f"Resumo Claude: {resumo}" if resumo else ""
    elif tipo == "handoff":
        head = f"⚠️ Lead {nome_label} ({telefone}) — PRECISA DE VOCÊ"
        body = f"Motivo: {motivo or 'não especificado'}"
        extra = f'Última msg: "{ultima_msg}"'
    elif tipo == "turnos":
        head = f"⏸ Lead {nome_label} ({telefone}) — 20 turnos sem progresso"
        body = f'Última msg: "{ultima_msg}"'
        extra = ""
    elif tipo == "claude_erro":
        head = f"⚠️ Lead {nome_label} ({telefone}) — Claude retornou JSON inválido"
        body = "Verifique a conversa; o lead segue em em_conversa para retry."
        extra = ""
    else:
        raise ValueError(f"unknown notification type: {tipo}")

    link = f"https://app.jurichat.com/conversation/{conversation_id}"

    parts = [head, body]
    if extra:
        parts.append(extra)
    parts.append(f"Link: {link}")
    return "\n".join(parts)


async def notify_mario(
    client: JurichatClient,
    *,
    mario_conversation_id: str,
    mensagem: str,
) -> None:
    """Send notification message to Mario via Jurichat.

    ``mario_conversation_id`` is a conversation with Mario's own number,
    pre-configured. Failures are logged but NOT raised — notifications
    are fire-and-forget per spec §9.

    Catches the full HTTP error surface (``OutboundError`` for exhausted
    retries, ``HTTPStatusError`` for non-retryable 4xx like a wrong
    ``MARIO_CONVERSATION_ID``, ``RequestError`` for transport failures).
    """
    try:
        # Pré-requisito: a conversa do Mario também precisa estar em
        # human-support mode. Idempotente.
        await client.start_human_support(mario_conversation_id)
        await client.send_message(mario_conversation_id, mensagem)
    except (OutboundError, httpx.HTTPStatusError, httpx.RequestError) as exc:
        logger.error("notify_mario failed: %s", exc)
