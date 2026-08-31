"""Plano B da área (31/ago) — v4: aplicar etiqueta EXISTENTE via API.

Descobertas até aqui: POST /tag valida (name, inboxId, color,
modules∈{crm,conversas,pessoas,fast-messages,tickets}) mas devolve 500 com
a nossa chave — criação fica no painel (ação única do Mario). O que o bot
precisa é APLICAR etiqueta já existente à conversa, e é isso que este
teste verifica, com leitura de confirmação após cada tentativa.

Pré-requisito: Mario cria a etiqueta "Teste Julia" no painel (Gerenciar
etiquetas). Depois:

    cd /opt/noviello-funil-saude && git pull && .venv/bin/python scripts/testar_etiqueta_area.py
"""

import asyncio
import json

import httpx

from noviello_funil.config import Settings

CONVERSA_MARIO_EU = "cmryyxpwx00vlql0ixlyneefl"
NOME_ETIQUETA = "Teste Julia"


async def _tags_da_conversa(c: httpx.AsyncClient) -> list:
    r = await c.get(f"/conversation/{CONVERSA_MARIO_EU}")
    d = r.json().get("data") or {}
    return d.get("tags") or []


def _tem_tag(tags: list, tag_id: str) -> bool:
    for t in tags:
        if t == tag_id:
            return True
        if isinstance(t, dict) and tag_id in (t.get("id"), t.get("tagId")):
            return True
    return False


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        r = await c.get("/tag", params={"inboxId": s.jurichat_inbox_id})
        catalogo = r.json() if r.status_code == 200 else []
        if isinstance(catalogo, dict):
            catalogo = catalogo.get("data") or []
        print(f"GET /tag -> {r.status_code}, {len(catalogo)} etiqueta(s): "
              f"{json.dumps(catalogo, ensure_ascii=False)[:400]}")
        tag_id = ""
        for t in catalogo:
            if isinstance(t, dict) and t.get("name") == NOME_ETIQUETA:
                tag_id = t.get("id") or ""
        if not tag_id:
            print(f"\nEtiqueta '{NOME_ETIQUETA}' não existe ainda — crie no "
                  "painel (Gerenciar etiquetas) e rode de novo.")
            return
        print(f"tag_id: {tag_id}")

        tentativas = (
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}",
             {"tagIds": [tag_id]}),
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}",
             {"tags": [tag_id]}),
            ("POST", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"tagId": tag_id}),
            ("POST", f"/conversation/{CONVERSA_MARIO_EU}/tags",
             {"tagId": tag_id}),
            ("POST", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"id": tag_id}),
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"tagId": tag_id}),
            ("PUT", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"tagId": tag_id}),
            ("POST", f"/tag/{tag_id}/conversation",
             {"conversationId": CONVERSA_MARIO_EU}),
        )
        aplicou = ""
        for metodo, rota, body in tentativas:
            r = await c.request(metodo, rota, json=body)
            tags = await _tags_da_conversa(c)
            pegou = _tem_tag(tags, tag_id)
            print(f"{metodo:5} {rota:44} -> {r.status_code}; "
                  f"tags: {json.dumps(tags)[:100]} {'<<< PEGOU' if pegou else ''}")
            if pegou:
                aplicou = f"{metodo} {rota} {json.dumps(body)}"
                break

        if not aplicou:
            print("\nNenhuma forma de aplicar pegou — reporto e pensamos "
                  "no próximo passo.")
            return
        print(f"\nFUNCIONA: {aplicou}")

        # Limpeza: tira a etiqueta da conversa de teste (a etiqueta fica).
        for metodo, rota, body in (
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}", {"tagIds": []}),
            ("DELETE", f"/conversation/{CONVERSA_MARIO_EU}/tag/{tag_id}", None),
        ):
            r = await c.request(metodo, rota, json=body)
            tags = await _tags_da_conversa(c)
            print(f"limpeza {metodo} -> {r.status_code}; tags: "
                  f"{json.dumps(tags)[:80]}")
            if not _tem_tag(tags, tag_id):
                print("Conversa de teste limpa.")
                break


if __name__ == "__main__":
    asyncio.run(main())
