"""Sonda a API do Jurichat atrás do vínculo de ÁREA da conversa (30/ago).

v2: a v1 mostrou que o detalhe vem embrulhado em ``data`` (top-level =
['data','hasMore']) e que nenhum nome óbvio de área existe no payload nem
no catálogo (só /departments respondeu, mas é o "Setores responsáveis").
Agora desembrulha o ``data``, lista TODAS as chaves com amostra do valor
(pra achar o campo seja qual for o nome) e tenta mais rotas de catálogo,
inclusive as escopadas na conversa. Continua 100% leitura.

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

_ROTAS_CATALOGO = (
    "/crm", "/crms", "/board", "/boards", "/funnel", "/funnels",
    "/pipeline", "/pipelines", "/vinculo", "/vinculos", "/link", "/links",
    "/tag", "/tags", "/field", "/fields", "/customField", "/customFields",
    "/areaOfLaw", "/areasOfLaw", "/legalArea", "/legalAreas",
    "/especialidade", "/especialidades", "/subject", "/subjects",
    "/topic", "/topics", "/service", "/services", "/product", "/products",
)

_ROTAS_DA_CONVERSA = (
    "/conversation/{cid}/area", "/conversation/{cid}/areas",
    "/conversation/{cid}/link", "/conversation/{cid}/links",
    "/conversation/{cid}/crm", "/conversation/{cid}/vinculo",
    "/conversation/{cid}/tags", "/conversation/{cid}/fields",
)


def _amostra(valor) -> str:
    dump = json.dumps(valor, ensure_ascii=False, default=str)
    return dump[:160]


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        primeiro_cid = next(iter(CONVERSAS.values()))
        for nome, cid in CONVERSAS.items():
            r = await c.get(f"/conversation/{cid}")
            print(f"\n=== {nome} — GET /conversation/{{id}} -> {r.status_code}")
            if r.status_code != 200:
                print(r.text[:200])
                continue
            d = r.json().get("data") or {}
            for chave in sorted(d.keys()):
                if chave == "messages":
                    print(f"  {chave}: [{len(d[chave] or [])} mensagens — omitidas]")
                    continue
                print(f"  {chave}: {_amostra(d[chave])}")

        print("\n=== catálogo — tentativas de GET")
        for rota in _ROTAS_CATALOGO:
            r = await c.get(rota)
            corpo = "" if r.status_code == 404 else " " + r.text[:160].replace("\n", " ")
            print(f"{rota:18} -> {r.status_code}{corpo}")

        print("\n=== rotas escopadas na conversa (Lia) — tentativas de GET")
        for molde in _ROTAS_DA_CONVERSA:
            rota = molde.format(cid=primeiro_cid)
            r = await c.get(rota)
            corpo = "" if r.status_code == 404 else " " + r.text[:160].replace("\n", " ")
            print(f"{molde:32} -> {r.status_code}{corpo}")


if __name__ == "__main__":
    asyncio.run(main())
