"""Sonda a API do Jurichat atrás do vínculo de ÁREA da conversa (31/ago).

v5: a listagem com os parâmetros do painel veio VAZIA pra chave de API (o
painel usa a sessão do usuário) — mas o detalhe da conversa tem um campo
``group`` que nunca inspecionamos de perto. Hipótese: "Vincular área" =
esse group (a escrita observada foi ``PATCH /conversation/{id}``; o body
seria algo como groupId). Imprime o valor COMPLETO de group, tags e
priority das 3 conversas-gabarito. 100% leitura.

Uso (VPS):
    cd /opt/noviello-funil-saude && git pull && .venv/bin/python scripts/sondar_area_jurichat.py
"""

import asyncio
import json

import httpx

from noviello_funil.config import Settings

# Conversas marcadas na mão por Mario em 30/ago (gabarito).
CONVERSAS = {
    "Lia (Saude)": "cmtgml91305spqr075cosiy7i",
    "Kayan (Trib-Imob)": "cmtd5tdin18ozpa06grf6bcfq",
    "Maria (Sucessoes)": "cmtd7o14h1k68pa06o5bfiuhr",
}


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        for nome, cid in CONVERSAS.items():
            r = await c.get(f"/conversation/{cid}")
            print(f"\n=== {nome} -> {r.status_code}")
            if r.status_code != 200:
                print(r.text[:200])
                continue
            d = r.json().get("data") or {}
            for chave in ("group", "tags", "priority", "status"):
                valor = json.dumps(d.get(chave), ensure_ascii=False, default=str)
                print(f"  {chave}: {valor[:600]}")


if __name__ == "__main__":
    asyncio.run(main())
