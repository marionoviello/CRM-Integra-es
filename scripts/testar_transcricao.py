"""Smoke: transcreve o ÚLTIMO áudio de uma conversa via Groq — prova o pipeline
(download + Whisper) sem depender do bot responder. Útil porque o bot pula o canal
de notificação do Mario, então mandar áudio pra si mesmo não exercita o fluxo.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/testar_transcricao.py <ID_DA_CONVERSA>
"""

import asyncio
import sys

import httpx

from noviello_funil.config import Settings
from noviello_funil.transcricao import transcrever_audio


async def main() -> None:
    cid = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if "id=" in cid:
        cid = cid.split("id=")[-1]
    cid = cid.split("&")[0].split("?")[0].strip()
    if not cid:
        print("uso: testar_transcricao.py <ID_DA_CONVERSA>")
        return
    s = Settings()
    if not s.groq_api_key:
        print("❌ GROQ_API_KEY vazio no .env — configure antes.")
        return
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.get(
            f"{s.jurichat_base_url.rstrip('/')}/conversation/{cid}",
            headers={"x-jurichat-api-key": s.jurichat_api_key},
        )
        if r.status_code >= 400:
            print(f"❌ conversa: HTTP {r.status_code}")
            return
        d = r.json()
        msgs = (d.get("data") or d).get("messages") or []
        audios = [m for m in msgs if m.get("type") == "audio"]
        if not audios:
            print("nenhum áudio nessa conversa.")
            return
        url = audios[-1].get("content") or ""
        print(f"áudio: {url[:72]}...")
        texto = await transcrever_audio(url, groq_key=s.groq_api_key, http=http)
        print("\n=== TRANSCRIÇÃO (Groq Whisper) ===")
        print(repr(texto))
        print("\n✅ pipeline de transcrição OK" if texto
              else "\n❌ falhou — veja o warning acima (chave/formato/rede)")


if __name__ == "__main__":
    asyncio.run(main())
