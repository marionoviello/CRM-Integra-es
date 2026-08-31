"""Teste SEGURO da escrita do vínculo de área via API (31/ago).

O DevTools do Mario revelou a escrita do painel::

    PATCH /conversation/{id}
    body {"legalAreaId": "cmptwamgp0023wly34mr437kv"}   # Direito da Saúde

Este script aplica esse mesmo PATCH na conversa "Mario eu" (canal de
alerta — sem cliente envolvido, reversível pelo painel em "Desvincular
área"). ATENÇÃO: este endpoint já devolveu 200 SEM EFEITO no passado
(caso do archive em 10/jul), então o 200 aqui não prova nada sozinho —
a prova é abrir o painel e ver se a Área "Direito da Saúde" apareceu na
conversa "Mario eu".
"""

import asyncio
import json

import httpx

from noviello_funil.config import Settings

AREA_DIREITO_DA_SAUDE = "cmptwamgp0023wly34mr437kv"
CONVERSA_MARIO_EU = "cmryyxpwx00vlql0ixlyneefl"


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        r = await c.patch(
            f"/conversation/{CONVERSA_MARIO_EU}",
            json={"legalAreaId": AREA_DIREITO_DA_SAUDE},
        )
        print(f"PATCH -> {r.status_code} {r.text[:200]!r}")

        r = await c.get(f"/conversation/{CONVERSA_MARIO_EU}")
        d = r.json().get("data") or {}
        d.pop("messages", None)
        legais = {k: v for k, v in d.items() if "legal" in k.lower()}
        print("chaves com 'legal' no detalhe:", json.dumps(
            legais, ensure_ascii=False, default=str) or "{}")
        print("group:", json.dumps(d.get("group"), ensure_ascii=False))
        print("\nAgora abra o painel na conversa 'Mario eu' e confira se a "
              "Área 'Direito da Saúde' apareceu — isso é o que decide.")


if __name__ == "__main__":
    asyncio.run(main())
