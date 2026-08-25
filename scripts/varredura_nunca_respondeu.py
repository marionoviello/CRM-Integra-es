"""Varredura completa do Jurichat: leads que NUNCA responderam (zero mensagem
INBOUND em todo o histórico) e ainda não estão arquivados.

Cobre TODAS as conversas do inbox via API — não só as que já passaram pelo
nosso funil local. Usa os mesmos parâmetros que o próprio painel do Jurichat
usa (inboxId + integrationId), descobertos via inspeção de rede em
2026-07-03: sem ``integrationId`` o endpoint ``GET /conversation`` volta
lista vazia (drift silencioso da API — não documentado em lugar nenhum).

Não filtra por "encerrado" (Jurichat não expõe um enum de status conhecido
pra isso) — a coluna ``status`` vem no relatório pra conferência manual.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/varredura_nunca_respondeu.py
"""

import asyncio

import httpx

from noviello_funil.config import Settings

# Reverse-engineered do painel real (Chrome DevTools) em 2026-07-03.
INBOX_ID = "cmhphehs612ucpp0ilvlf0c9v"
INTEGRATION_ID = "cmnt2le7702tzqt0iyfd52uty"


async def listar_todas_conversas(
    http: httpx.AsyncClient, api_key: str, base_url: str,
) -> list[dict]:
    conversas: list[dict] = []
    page = 1
    while True:
        resp = await http.get(
            f"{base_url}/conversation",
            headers={"x-jurichat-api-key": api_key},
            params={
                "page": str(page), "limit": "100",
                "inboxId": INBOX_ID, "integrationId": INTEGRATION_ID,
                "showGroups": "true", "onlyGroups": "false",
                "showOnlyUnread": "false", "onlyArchived": "false",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data") or []
        conversas.extend(items)
        total_pages = int(data.get("totalPages") or 1)
        print(f"  pagina {page}/{total_pages} — {len(items)} conversas")
        if page >= total_pages or not items:
            break
        page += 1
    return conversas


async def nunca_respondeu(
    http: httpx.AsyncClient, api_key: str, base_url: str, conv_id: str,
) -> bool | None:
    resp = await http.get(
        f"{base_url}/conversation/{conv_id}",
        headers={"x-jurichat-api-key": api_key},
    )
    if resp.status_code >= 400:
        return None  # não dá pra confirmar — não afirma "nunca respondeu"
    data = resp.json()
    msgs = (data.get("data") or data).get("messages") or []
    return not any(m.get("direction") == "INBOUND" for m in msgs)


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(timeout=30) as http:
        print("Buscando todas as conversas nao-arquivadas...")
        conversas = await listar_todas_conversas(
            http, s.jurichat_api_key, s.jurichat_base_url,
        )
        conversas = [c for c in conversas if not c.get("isGroup")]
        print(f"Total: {len(conversas)} conversas (individuais, nao-arquivadas).\n")

        # Pre-filtro barato: se a ULTIMA mensagem ja e do lead, obviamente
        # ele respondeu -> descarta sem gastar uma chamada extra.
        candidatas = [
            c for c in conversas
            if (c.get("lastMessage") or {}).get("direction") != "INBOUND"
        ]
        print(
            f"Candidatas (ultima msg nao e do lead): {len(candidatas)} "
            "— checando historico completo...\n"
        )

        achados = []
        nao_verificadas = 0
        for i, c in enumerate(candidatas, 1):
            resultado = await nunca_respondeu(
                http, s.jurichat_api_key, s.jurichat_base_url, c["id"],
            )
            if resultado is None:
                nao_verificadas += 1
            elif resultado:
                pessoa = c.get("person") or {}
                tags = ", ".join(t.get("name", "") for t in (c.get("tags") or []))
                achados.append({
                    "nome": pessoa.get("name") or "(sem nome)",
                    "telefone": pessoa.get("phoneNumber") or "",
                    "status": c.get("status") or "",
                    "tags": tags,
                    "criado_em": c.get("createdAt") or "",
                    "conversation_id": c["id"],
                })
            if i % 20 == 0:
                print(f"  ...{i}/{len(candidatas)} checadas")
            await asyncio.sleep(0.1)  # nao martelar a API

        achados.sort(key=lambda a: a["criado_em"])
        print(f"\n=== {len(achados)} leads NUNCA responderam e nao estao arquivados ===")
        if nao_verificadas:
            print(f"({nao_verificadas} conversas nao verificaveis — erro ao buscar detalhe)\n")
        for a in achados:
            print(
                f"{a['criado_em']:<25} {a['nome']:<28} {a['telefone']:<16} "
                f"status={a['status']:<20} tags=[{a['tags']}]"
            )


if __name__ == "__main__":
    asyncio.run(main())
