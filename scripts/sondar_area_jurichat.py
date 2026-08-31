"""Sonda a API do Jurichat atrás do vínculo de ÁREA da conversa (31/ago).

v4: o DevTools do Mario revelou (a) a ESCRITA — ``PATCH /conversation/{id}``
(falta o nome do campo no body) — e (b) os parâmetros REAIS da listagem do
painel: ``/conversation?page=1&limit=100&inboxId=...&integrationId=...``.
A listagem da página 1 tem 153 kB pra ~100 conversas, então o item deve vir
completo — muito provavelmente com o vínculo de área dentro. Esta sonda usa
esses parâmetros, acha as 3 conversas-gabarito e imprime o item inteiro.
100% leitura.

Uso (VPS):
    cd /opt/noviello-funil-saude && git pull && .venv/bin/python scripts/sondar_area_jurichat.py
"""

import asyncio
import json

import httpx

from noviello_funil.config import Settings

# Conversas marcadas na mão por Mario em 30/ago (gabarito).
CONVERSAS = {
    "cmtgml91305spqr075cosiy7i": "Lia (Saude)",
    "cmtd5tdin18ozpa06grf6bcfq": "Kayan (Trib-Imob)",
    "cmtd7o14h1k68pa06o5bfiuhr": "Maria (Sucessoes)",
}
# Integração "Noviello Fixo" — o painel manda integrationId na listagem.
INTEGRATION_ID = "cmryywd6o00mrql0ibhvz90ma"


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=30,
    ) as c:
        variantes = [
            {"page": "1", "limit": "100", "inboxId": s.jurichat_inbox_id,
             "integrationId": INTEGRATION_ID},
            {"page": "1", "limit": "100", "inboxId": s.jurichat_inbox_id,
             "integrationId": INTEGRATION_ID, "unread": "false"},
            {"page": "1", "limit": "100", "inboxId": s.jurichat_inbox_id},
        ]
        itens: list = []
        for params in variantes:
            r = await c.get("/conversation", params=params)
            corpo = r.json() if r.status_code == 200 else {}
            data = corpo.get("data") if isinstance(corpo, dict) else corpo
            data = data if isinstance(data, list) else []
            print(f"GET /conversation {sorted(params)} -> {r.status_code}, "
                  f"{len(data)} itens")
            if data:
                itens = data
                break

        if not itens:
            print("Nenhuma variante devolveu itens — colar o Payload do PATCH "
                  "no DevTools continua sendo o caminho.")
            return

        exemplo = itens[0]
        print("\nchaves de um item da listagem:", sorted(exemplo.keys()))

        achou = 0
        for item in itens:
            rotulo = CONVERSAS.get(item.get("id") or "")
            if not rotulo:
                continue
            achou += 1
            enxuto = {
                k: v for k, v in item.items()
                if k not in ("messages", "participants")
            }
            print(f"\n=== {rotulo} — item completo (sem messages):")
            print(json.dumps(enxuto, ensure_ascii=False, indent=1)[:2200])
        if not achou:
            print("\nGabaritos não estavam na página 1 — aumentar limit/page.")


if __name__ == "__main__":
    asyncio.run(main())
