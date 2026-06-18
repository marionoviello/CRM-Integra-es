#!/usr/bin/env python
"""Envia UMA mensagem no Jurichat pela API do bot (start_human_support + send).

Tira o atrito de "colar à mão" nas remarcações: o Claude monta a mensagem,
você roda UM comando. Faz ``start_human_support`` (idempotente, pré-requisito
do send) e depois ``send_message`` (que já passa pelo sanitizer de marca).

Uso (no VPS):
    .venv/bin/python scripts/enviar_jurichat.py --conversa <id> --texto "mensagem"
    # ou de um arquivo (útil pra mensagens longas/multilinha):
    .venv/bin/python scripts/enviar_jurichat.py --conversa <id> --arquivo msg.txt

Requer JURICHAT_API_KEY no .env. Envia EXATAMENTE 1 mensagem — não é loop.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from noviello_funil.config import Settings
from noviello_funil.outbound import JurichatClient


async def _enviar(settings: Settings, conversa: str, texto: str) -> None:
    jc = JurichatClient(
        settings.jurichat_api_key,
        settings.jurichat_base_url,
        bot_user_id=settings.jurichat_bot_user_id,
    )
    try:
        # pré-requisito: sem isso o send-message retorna 400
        await jc.start_human_support(conversa)
        await jc.send_message(conversa, texto)
    finally:
        await jc.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Envia uma mensagem no Jurichat pela API do bot.",
    )
    ap.add_argument("--conversa", required=True,
                    help="jurichat_conversation_id da conversa")
    grupo = ap.add_mutually_exclusive_group(required=True)
    grupo.add_argument("--texto", help="texto da mensagem")
    grupo.add_argument("--arquivo", help="arquivo .txt com o texto da mensagem")
    args = ap.parse_args()

    texto = args.texto
    if args.arquivo:
        with open(args.arquivo, encoding="utf-8") as f:
            texto = f.read().strip()
    if not (texto or "").strip():
        print("ERRO: mensagem vazia", file=sys.stderr)
        return 2

    settings = Settings()
    if not settings.jurichat_api_key:
        print("ERRO: JURICHAT_API_KEY ausente no .env", file=sys.stderr)
        return 2

    asyncio.run(_enviar(settings, args.conversa, texto))
    print(f"OK — mensagem enviada na conversa {args.conversa}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
