"""Plano B da área (31/ago): a chave de API consegue usar ETIQUETAS?

A Área oficial só é gravável pela sessão do painel (comprovado: PATCH
200-falso + todas as sub-rotas 404). Etiquetas são a alternativa: GET
/tag?inboxId responde à nossa chave e o detalhe da conversa traz ``tags``
— escrita VERIFICÁVEL por código, sem depender do painel.

Este teste, na conversa "Mario eu" (reversível):
1. cria a etiqueta "Teste Julia" (várias formas de body; o 400 do
   Fastify lista os campos obrigatórios e nos guia);
2. tenta aplicá-la à conversa (PATCH direto e sub-rotas), conferindo o
   ``tags`` do detalhe após CADA tentativa — para no primeiro sucesso;
3. tenta a limpeza (tirar da conversa e apagar a etiqueta) e reporta.
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


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        # 1. Criar a etiqueta.
        tag_id = ""
        for body in (
            {"name": NOME_ETIQUETA, "inboxId": s.jurichat_inbox_id},
            {"name": NOME_ETIQUETA, "inboxId": s.jurichat_inbox_id,
             "color": "#1faf54"},
            {"name": NOME_ETIQUETA},
        ):
            r = await c.post("/tag", json=body)
            print(f"POST /tag {sorted(body)} -> {r.status_code} {r.text[:200]}")
            if r.status_code < 300:
                corpo = r.json()
                dado = corpo.get("data") if isinstance(corpo, dict) else corpo
                if isinstance(dado, dict):
                    tag_id = dado.get("id") or ""
                break
        if not tag_id:
            # Pode já existir de uma rodada anterior — procura no catálogo.
            r = await c.get("/tag", params={"inboxId": s.jurichat_inbox_id})
            for t in (r.json() if r.status_code == 200 else []) or []:
                if isinstance(t, dict) and t.get("name") == NOME_ETIQUETA:
                    tag_id = t.get("id") or ""
        print(f"tag_id: {tag_id or 'NAO CONSEGUI CRIAR/ACHAR'}")
        if not tag_id:
            return

        # 2. Aplicar à conversa — para na primeira forma que "pega".
        tentativas = (
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}",
             {"tagIds": [tag_id]}),
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}",
             {"tags": [tag_id]}),
            ("POST", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"tagId": tag_id}),
            ("POST", f"/conversation/{CONVERSA_MARIO_EU}/tags",
             {"tagId": tag_id}),
            ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"tagId": tag_id}),
            ("PUT", f"/conversation/{CONVERSA_MARIO_EU}/tag",
             {"tagId": tag_id}),
        )
        aplicou = ""
        for metodo, rota, body in tentativas:
            r = await c.request(metodo, rota, json=body)
            tags = await _tags_da_conversa(c)
            pegou = any(
                (t.get("id") if isinstance(t, dict) else t) == tag_id
                or (isinstance(t, dict) and t.get("tagId") == tag_id)
                for t in tags
            )
            print(f"{metodo} {rota.split('/')[-1]:4} {sorted(body)} -> "
                  f"{r.status_code}; tags depois: {json.dumps(tags)[:120]} "
                  f"{'<<< PEGOU' if pegou else ''}")
            if pegou:
                aplicou = f"{metodo} {rota} {json.dumps(body)}"
                break

        if aplicou:
            print(f"\nFUNCIONA: {aplicou}")
        else:
            print("\nNenhuma forma de aplicar etiqueta pegou.")

        # 3. Limpeza best-effort (tira da conversa e apaga a etiqueta).
        if aplicou:
            for metodo, rota, body in (
                ("PATCH", f"/conversation/{CONVERSA_MARIO_EU}",
                 {"tagIds": []}),
                ("DELETE", f"/conversation/{CONVERSA_MARIO_EU}/tag/{tag_id}",
                 None),
            ):
                r = await c.request(metodo, rota, json=body)
                tags = await _tags_da_conversa(c)
                print(f"limpeza {metodo} -> {r.status_code}; tags: "
                      f"{json.dumps(tags)[:80]}")
                if not tags:
                    break
        r = await c.delete(f"/tag/{tag_id}")
        print(f"DELETE /tag/{{id}} -> {r.status_code} {r.text[:120]}")


if __name__ == "__main__":
    asyncio.run(main())
