"""Tests for the outbound HTTP layer."""

import httpx
import pytest
import respx  # noqa: F401  # used in T07

from noviello_funil.outbound import (
    JurichatClient,  # noqa: F401  # used in T07
    OutboundError,
    with_retry,
)


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
