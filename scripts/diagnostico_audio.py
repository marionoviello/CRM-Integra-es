"""Diagnóstico: como uma mensagem de ÁUDIO chega na API da Jurichat.

Mostra, das mensagens NÃO-texto: type, se content/transcription vêm preenchidos,
as chaves do metadata, e quaisquer campos com URL/arquivo (onde está o áudio pra
baixar e transcrever). Define o caminho do fix de áudio do bot.

A lista de conversas anda voltando vazia (issue à parte), então aceita o ID de UMA
conversa como argumento (abre a conversa com áudio no Jurichat, copia o id da URL
depois de ?id=) — assim baixa só ela, sem depender da lista.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/diagnostico_audio.py <ID_DA_CONVERSA>
    (sem o id, ele tenta varrer a lista)
"""

import asyncio
import json
import sys

import httpx

from noviello_funil.config import Settings
from noviello_funil.outbound import JurichatClient


def _campos_url(m: dict) -> dict:
    out = {}
    for k, v in m.items():
        if "url" in k.lower() or "media" in k.lower() or "file" in k.lower():
            out[k] = str(v)[:110]
        elif isinstance(v, str) and v.startswith("http"):
            out[k] = v[:110]
    return out


async def main() -> None:
    cid_arg = sys.argv[1].strip() if len(sys.argv) > 1 else None
    s = Settings()
    client = JurichatClient(
        s.jurichat_api_key, s.jurichat_base_url, bot_user_id=s.jurichat_bot_user_id,
    )
    raw = httpx.AsyncClient(
        base_url=s.jurichat_base_url.rstrip("/"),
        headers={"x-jurichat-api-key": s.jurichat_api_key}, timeout=30,
    )
    try:
        if cid_arg:
            ids = [cid_arg]
            print(f"conversa única: {cid_arg}")
        else:
            convs = await client.list_active_conversations(inbox_id=s.jurichat_inbox_id)
            print(f"conversas na lista: {len(convs)}")
            ids = [cv["id"] for cv in convs if cv.get("id") and not cv.get("isGroup")]

        tipos: dict[str, int] = {}
        achou = 0
        for cid in ids:
            r = await raw.get(f"/conversation/{cid}")
            if r.status_code >= 400:
                print(f"  conversa {cid}: HTTP {r.status_code}")
                continue
            await asyncio.sleep(0.12)
            d = r.json()
            for m in (d.get("data") or d).get("messages") or []:
                t = m.get("type") or "?"
                tipos[t] = tipos.get(t, 0) + 1
                if t != "text" and achou < 4:
                    info = {
                        "type": t,
                        "direction": m.get("direction"),
                        "content_preview": str(m.get("content"))[:110],
                        "transcription_preenchido": bool(m.get("transcription")),
                        "metadata_keys": list((m.get("metadata") or {}).keys()),
                        "campos_url": _campos_url(m),
                        "todos_os_campos": sorted(m.keys()),
                    }
                    print(f"\n=== msg NÃO-texto #{achou + 1} ===")
                    print(json.dumps(info, ensure_ascii=False, indent=1))
                    achou += 1
            if achou >= 4:
                break
        print(f"\nTIPOS de mensagem vistos: {tipos}")
    finally:
        await client.aclose()
        await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
