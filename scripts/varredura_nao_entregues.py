"""Incidente 26/jun — varre as conversas e RELATA mensagens OUTBOUND que a Jurichat
marcou como NÃO ENTREGUES (falha de WhatsApp durante o apagão do celular).

SÓ RELATA — não reenvia nada. O Mario revisa a lista; depois a gente reenvia com
cuidado (sem duplicar). Exclui o Joedson (pedido do Mario), arquivadas e grupos.

Auto-descobre o schema: imprime os CAMPOS de uma OUTBOUND (pra achar o campo de
status) e marca como falha qualquer mensagem cujo campo de STATUS (não o texto)
bata com 'fail/undeliver/error/reject/invalid'.

Uso no VPS:
    cd /opt/noviello-funil-saude && .venv/bin/python scripts/varredura_nao_entregues.py
"""

import asyncio

import httpx

from noviello_funil.config import Settings
from noviello_funil.outbound import JurichatClient

_EXCLUIR = {"cmo98fprx300jlh0716a8h0i2"}  # Joedson — NÃO mexer
_FALHA = ("fail", "undeliver", "not_delivered", "error", "invalid", "reject")
# campos de TEXTO da mensagem — não usar na heurística (o texto pode conter
# "erro"/"falha" e dar falso positivo).
_TEXTO = {"content", "text", "body", "message", "caption"}


def _campo_de_falha(msg: dict) -> str | None:
    for k, v in msg.items():
        if k.lower() in _TEXTO or not isinstance(v, str):
            continue
        if any(f in v.lower() for f in _FALHA):
            return f"{k}={v}"
    return None


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
        convs = await client.list_active_conversations(s.jurichat_inbox_id)
        print(f"conversas varridas: {len(convs)}")
        schema_visto = False
        total = 0
        for cv in convs:
            cid = cv.get("id")
            if not cid or cid in _EXCLUIR or cv.get("isGroup"):
                continue
            r = await raw.get(f"/conversation/{cid}")
            if r.status_code >= 400:
                continue
            d = r.json()
            msgs = (d.get("data") or d).get("messages") or []
            if not schema_visto:
                for m in msgs:
                    if m.get("direction") == "OUTBOUND":
                        print("CAMPOS de uma OUTBOUND:", sorted(m.keys()))
                        schema_visto = True
                        break
            falhas = [
                m for m in msgs
                if m.get("direction") == "OUTBOUND" and _campo_de_falha(m)
            ]
            if falhas:
                nome = (cv.get("person") or {}).get("name") or cid
                print(f"\n### {nome}  (conv {cid}) — {len(falhas)} falha(s)")
                for m in falhas[-5:]:
                    print(f"  • [{_campo_de_falha(m)}] {str(m.get('content'))[:110]}")
                total += len(falhas)
        print(f"\n=== TOTAL de OUTBOUND não-entregues (fora Joedson): {total} ===")
        print("(SÓ relatório — nada foi reenviado. Revise e me diga.)")
    finally:
        await client.aclose()
        await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
