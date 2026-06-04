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

    async def send_message(
        self,
        conversation_id: str,
        text: str,
        *,
        base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """POST /conversation/send-message (multipart/form-data)."""

        async def op() -> dict[str, Any]:
            resp = await self._client.post(
                f"{self._base_url}/conversation/send-message",
                data={"conversation_id": conversation_id, "text": text},
            )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

    async def get_conversation(
        self, conversation_id: str, *, base_delay: float = 1.0,
    ) -> dict[str, Any]:
        """GET /conversation/{id} — returns full conversation including transcription."""

        async def op() -> dict[str, Any]:
            resp = await self._client.get(
                f"{self._base_url}/conversation/{conversation_id}"
            )
            resp.raise_for_status()
            return resp.json()

        return await with_retry(op, attempts=3, base_delay=base_delay)

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
