"""Tests for the dead-man's switch ping (scheduler.ping_healthcheck)."""

import httpx
import pytest

from noviello_funil.scheduler import ping_healthcheck


@pytest.mark.asyncio
async def test_ping_calls_url(respx_mock):
    route = respx_mock.get("https://hc-ping.com/abc-123").mock(
        return_value=httpx.Response(200, text="OK"),
    )
    await ping_healthcheck("https://hc-ping.com/abc-123")
    assert route.called


@pytest.mark.asyncio
async def test_ping_empty_url_is_noop(respx_mock):
    """URL vazio = feature off — nenhuma request sai."""
    await ping_healthcheck("")
    assert not respx_mock.calls


@pytest.mark.asyncio
async def test_ping_failure_never_raises(respx_mock, caplog):
    """Healthchecks fora do ar NÃO pode derrubar o ciclo do scheduler."""
    respx_mock.get("https://hc-ping.com/down").mock(
        side_effect=httpx.ConnectError("connection refused"),
    )
    await ping_healthcheck("https://hc-ping.com/down")  # MUST NOT raise
    assert any("healthcheck ping falhou" in r.message for r in caplog.records)
