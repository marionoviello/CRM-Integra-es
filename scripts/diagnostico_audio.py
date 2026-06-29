"""Diagnóstico: como uma mensagem de ÁUDIO chega na API da Jurichat.

Acha as primeiras mensagens NÃO-texto e mostra: type, se content/transcription
vêm preenchidos, as chaves do metadata, e quaisquer campos com URL/arquivo (onde
está o áudio pra baixar e transcrever). Define o caminho do fix de áudio do bot.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/diagnostico_audio.py
"""

import asyncio
import json

import httpx

from noviello_funil.config import Settings
from noviello_funil.outbound import JurichatClient


def _campos_url(m: dict) -> dict:
    out = {}
    for k, v in m.items():
        if "url" in k.lower() or "media" in k.lower() or "file" in k.lower():
            out[k] = str(v)[:100]
        elif isinstance(v, str) and v.startswith("http"):
            out[k] = v[:100]
    return out


async def main() -> None:
    s = Settings()
    client = JurichatClient(
        s.jurichat_api_key, s.jurichat_base_url, bot_user_id=s.jurichat_bot_user_id,
    )
    raw = httpx.AsyncClient(
        base_url=s.jurichat_base_url.rstrip("/"),
        headers={"x-jurichat-api-key": s.jurichat_api_key}, timeout=30,
    )
    try:
        convs = await client.list_active_conversations(inbox_id=s.jurichat_inbox_id)
        print(f"conversas: {len(convs)}")
        tipos: dict[str, int] = {}
        achou = 0
        for cv in convs:
            cid = cv.get("id")
            if not cid or cv.get("isGroup"):
                continue
            r = await raw.get(f"/conversation/{cid}")
            if r.status_code >= 400:
                continue
            await asyncio.sleep(0.12)
            d = r.json()
            for m in (d.get("data") or d).get("messages") or []:
                t = m.get("type") or "?"
                tipos[t] = tipos.get(t, 0) + 1
                if t != "text" and achou < 3:
                    info = {
                        "type": t,
                        "direction": m.get("direction"),
                        "content_preview": str(m.get("content"))[:100],
                        "transcription_preenchido": bool(m.get("transcription")),
                        "metadata_keys": list((m.get("metadata") or {}).keys()),
                        "campos_url": _campos_url(m),
                        "todos_os_campos": sorted(m.keys()),
                    }
                    print(f"\n=== msg NÃO-texto #{achou + 1} ===")
                    print(json.dumps(info, ensure_ascii=False, indent=1))
                    achou += 1
            if achou >= 3:
                break
        print(f"\nTIPOS de mensagem vistos: {tipos}")
    finally:
        await client.aclose()
        await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
