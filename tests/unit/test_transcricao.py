"""Áudio: transcrição via Groq (download + Whisper), best-effort."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from noviello_funil.transcricao import transcrever_audio


def _http(get_bytes=b"OGGfake", post_json=None, get_exc=None, post_exc=None):
    http = MagicMock()
    if get_exc:
        http.get = AsyncMock(side_effect=get_exc)
    else:
        http.get = AsyncMock(return_value=SimpleNamespace(
            content=get_bytes, raise_for_status=lambda: None,
        ))
    if post_exc:
        http.post = AsyncMock(side_effect=post_exc)
    else:
        http.post = AsyncMock(return_value=SimpleNamespace(
            json=lambda: (post_json or {"text": "olá quero regularizar meu imóvel"}),
            raise_for_status=lambda: None,
        ))
    return http


@pytest.mark.asyncio
async def test_transcreve_audio_ok():
    http = _http()
    texto = await transcrever_audio(
        "https://storage.googleapis.com/x/y", groq_key="gsk-test", http=http,
    )
    assert texto == "olá quero regularizar meu imóvel"
    # mandou o áudio baixado pro Groq, com modelo whisper + pt
    kwargs = http.post.call_args.kwargs
    assert "audio" in kwargs["files"]["file"][0]
    assert kwargs["data"]["model"].startswith("whisper")
    assert kwargs["data"]["language"] == "pt"
    assert kwargs["headers"]["Authorization"] == "Bearer gsk-test"


@pytest.mark.asyncio
async def test_sem_chave_ou_url_retorna_none():
    http = _http()
    assert await transcrever_audio("https://x/y", groq_key="", http=http) is None
    assert await transcrever_audio("", groq_key="k", http=http) is None
    http.get.assert_not_awaited()


@pytest.mark.asyncio
async def test_falha_no_download_ou_groq_retorna_none():
    # download falha
    assert await transcrever_audio(
        "https://x/y", groq_key="k", http=_http(get_exc=RuntimeError("404")),
    ) is None
    # Groq falha
    assert await transcrever_audio(
        "https://x/y", groq_key="k", http=_http(post_exc=RuntimeError("429")),
    ) is None


@pytest.mark.asyncio
async def test_transcricao_vazia_retorna_none():
    http = _http(post_json={"text": "   "})
    assert await transcrever_audio("https://x/y", groq_key="k", http=http) is None
