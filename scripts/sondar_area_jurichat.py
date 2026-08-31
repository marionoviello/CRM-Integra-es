"""Sonda a API do Jurichat atrás do vínculo de ÁREA da conversa (30/ago).

Mario marcou 3 conversas na mão pelo painel (Direito da Saúde, Direito
Tributário - Imobiliário, Direito das Sucessões / Inventário) pra servirem
de gabarito. Este script NÃO escreve nada: lê o detalhe das 3 conversas e
procura (a) onde o vínculo de área aparece no payload e (b) qual endpoint
lista o catálogo de áreas.

Uso (VPS):
    cd /opt/noviello-funil-saude && git pull && uv run python scripts/sondar_area_jurichat.py
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

_SUSPEITOS = (
    "area", "law", "setor", "sector", "vinculo", "specialt", "practice",
    "categ", "depart",
)

_ROTAS_CATALOGO = (
    "/area", "/areas", "/lawArea", "/lawAreas", "/law-area", "/law-areas",
    "/crm/area", "/crm/areas", "/setor", "/setores", "/sector", "/sectors",
    "/department", "/departments", "/category", "/categories",
)


def _achar_suspeitos(obj, caminho=""):
    """Percorre o JSON e coleta (caminho, valor) de chaves suspeitas."""
    achados = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{caminho}.{k}" if caminho else k
            if any(s in k.lower() for s in _SUSPEITOS):
                achados.append((p, v))
            achados.extend(_achar_suspeitos(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:3]):
            achados.extend(_achar_suspeitos(v, f"{caminho}[{i}]"))
    return achados


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        for nome, cid in CONVERSAS.items():
            r = await c.get(f"/conversation/{cid}")
            print(f"\n=== {nome} — GET /conversation/{{id}} -> {r.status_code}")
            if r.status_code != 200:
                print(r.text[:200])
                continue
            data = r.json()
            print("top-level keys:", sorted(data.keys()))
            person = data.get("person")
            if isinstance(person, dict):
                print("person keys:", sorted(person.keys()))
            for caminho, valor in _achar_suspeitos(data):
                if caminho.startswith("messages"):
                    continue
                dump = json.dumps(valor, ensure_ascii=False)[:250]
                print(f"  SUSPEITO {caminho} = {dump}")

        print("\n=== catálogo de áreas — tentativas de GET")
        for rota in _ROTAS_CATALOGO:
            try:
                r = await c.get(rota)
                corpo = r.text[:120].replace("\n", " ")
                print(f"{rota:15} -> {r.status_code} {corpo}")
            except Exception as exc:  # noqa: BLE001 — sonda: reporta e segue
                print(f"{rota:15} -> ERRO {exc}")


if __name__ == "__main__":
    asyncio.run(main())
