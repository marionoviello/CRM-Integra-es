"""Sonda a API do Jurichat atrás do vínculo de ÁREA da conversa (30/ago).

v3: a v2 revelou que /funnel e /tag EXISTEM (400 "inboxId Required") — a
"Área" do painel (bolinha colorida + "Alterar vínculo") deve ser o FUNIL
do CRM. Agora: lista os funis e tags da inbox, tenta rotas derivadas do
funil e cruza os ids/nomes descobertos com o payload das 3 conversas
marcadas, pra apontar exatamente onde o vínculo aparece. 100% leitura.

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


def _ids_e_nomes(obj) -> set[str]:
    """Coleta valores de id/name em qualquer nível do JSON."""
    achados: set[str] = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("id", "name") and isinstance(v, str) and len(v) > 3:
                achados.add(v)
            achados |= _ids_e_nomes(v)
    elif isinstance(obj, list):
        for v in obj:
            achados |= _ids_e_nomes(v)
    return achados


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        params = {"inboxId": s.jurichat_inbox_id}

        r = await c.get("/funnel", params=params)
        print(f"=== GET /funnel?inboxId=... -> {r.status_code}")
        print(r.text[:2500])
        funis = r.json() if r.status_code == 200 else {}
        marcadores = _ids_e_nomes(funis)

        r = await c.get("/tag", params=params)
        print(f"\n=== GET /tag?inboxId=... -> {r.status_code}")
        print(r.text[:800])

        # Rotas derivadas do funil (sem id e, se houver funil, com o 1º id).
        derivadas = ["/funnel/card", "/funnel/cards", "/funnel/step"]
        ids_funil = [m for m in marcadores if not m.startswith("Direito")]
        if ids_funil:
            fid = sorted(ids_funil)[0]
            derivadas += [f"/funnel/{fid}", f"/funnel/{fid}/card",
                          f"/funnel/{fid}/cards", f"/funnel/{fid}/steps"]
        print("\n=== rotas derivadas — tentativas de GET")
        for rota in derivadas:
            r = await c.get(rota, params=params)
            corpo = "" if r.status_code == 404 else " " + r.text[:200].replace("\n", " ")
            print(f"{rota:28} -> {r.status_code}{corpo}")

        # Cruzamento: os ids/nomes dos funis aparecem no detalhe da conversa?
        print("\n=== cruzamento funil × conversas marcadas")
        for nome, cid in CONVERSAS.items():
            r = await c.get(f"/conversation/{cid}")
            if r.status_code != 200:
                print(f"{nome}: GET detalhe -> {r.status_code}")
                continue
            d = r.json().get("data") or {}
            d.pop("messages", None)
            bruto = json.dumps(d, ensure_ascii=False, default=str)
            batidas = sorted(m for m in marcadores if m in bruto)
            print(f"{nome}: chaves={sorted(d.keys())}")
            print(f"  marcadores do funil presentes no payload: {batidas or 'NENHUM'}")


if __name__ == "__main__":
    asyncio.run(main())
