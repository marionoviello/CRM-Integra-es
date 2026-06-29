"""Transcrição dos áudios (voz) que o lead manda no WhatsApp — via Groq Whisper.

A Jurichat NÃO transcreve sozinha; entrega a mensagem com ``type='audio'`` e o
``content`` apontando pro arquivo no GCS. Aqui baixamos esse arquivo e mandamos pro
Groq (``whisper-large-v3``, OpenAI-compatível, grátis/rápido, ótimo PT-BR).

Best-effort: qualquer falha (download, API, formato) → None, e o bot cai no
comportamento atual ("não consigo ouvir áudio"). NUNCA levanta.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_MODELO = "whisper-large-v3"


async def transcrever_audio(url: str, *, groq_key: str, http: Any) -> str | None:
    """Baixa o áudio de ``url`` e devolve a transcrição (pt-br), ou None em falha.

    ``http`` é um httpx.AsyncClient (sem base_url — usamos URLs absolutas: o GCS
    do áudio e a API do Groq)."""
    if not url or not groq_key:
        return None
    try:
        r = await http.get(url)
        r.raise_for_status()
        audio = r.content
        if not audio:
            return None
        resp = await http.post(
            _GROQ_URL,
            headers={"Authorization": f"Bearer {groq_key}"},
            files={"file": ("audio.ogg", audio, "audio/ogg")},
            data={"model": _MODELO, "language": "pt", "response_format": "json"},
        )
        resp.raise_for_status()
        texto = (resp.json().get("text") or "").strip()
        return texto or None
    except Exception as exc:  # noqa: BLE001 — best-effort, nada vaza
        logger.warning("transcrever_audio falhou (%s...): %s", url[:60], exc)
        return None
