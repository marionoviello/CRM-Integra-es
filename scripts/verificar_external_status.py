"""Confere, na API REAL, o campo de status de entrega das mensagens OUTBOUND.

A detecção de não-entrega (caso Vizca, 20/jul) foi construída sobre o campo
``externalStatus`` — anotado de observação, NUNCA verificado num payload real.
Os testes mockam a Jurichat, e mock aceita qualquer corpo: foi exatamente assim
que o bug do ZapSign (signer_name) passou batido por dias.

Este script imprime TODOS os campos parecidos com status das mensagens
OUTBOUND, com os valores encontrados. Se ``externalStatus`` não aparecer aqui,
a detecção está inerte e o nome certo do campo está na lista.

Uso no VPS:
    cd /opt/noviello-funil-saude && \
      .venv/bin/python scripts/verificar_external_status.py <ID_OU_URL_DA_CONVERSA>
"""

import asyncio
import sys

import httpx

from noviello_funil.config import Settings
from noviello_funil.outbound import mensagens_nao_entregues

# Nomes de campo que valem inspeção (o certo pode ser qualquer um destes).
_CANDIDATOS = ("status", "state", "delivery", "error", "failed", "ack")


async def main() -> None:
    cid = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    if "id=" in cid:
        cid = cid.split("id=")[-1]
    cid = cid.split("&")[0].split("?")[0].strip()
    if not cid:
        print("uso: verificar_external_status.py <ID_OU_URL_DA_CONVERSA>")
        return

    s = Settings()
    async with httpx.AsyncClient(timeout=60) as http:
        r = await http.get(
            f"{s.jurichat_base_url.rstrip('/')}/conversation/{cid}",
            headers={"x-jurichat-api-key": s.jurichat_api_key},
        )
        if r.status_code >= 400:
            print(f"❌ conversa: HTTP {r.status_code} — {r.text[:200]}")
            return
        data = r.json()

    msgs = (data.get("data") or data).get("messages") or []
    out = [m for m in msgs if m.get("direction") == "OUTBOUND"]
    print(f"mensagens: {len(msgs)} total, {len(out)} OUTBOUND\n")
    if not out:
        print("sem OUTBOUND nesta conversa — escolha outra.")
        return

    print("CAMPOS da última OUTBOUND:")
    print("  " + ", ".join(sorted(out[-1].keys())) + "\n")

    achou_external = False
    print("CAMPOS de status por mensagem OUTBOUND (últimas 15):")
    for m in out[-15:]:
        campos = {
            k: v for k, v in m.items()
            if any(c in k.lower() for c in _CANDIDATOS)
        }
        if "externalStatus" in m:
            achou_external = True
        print(f"  {str(m.get('id'))[:24]:26} {campos}")

    print()
    if achou_external:
        valores = sorted({str(m.get("externalStatus")) for m in out})
        print(f"✅ externalStatus EXISTE. Valores vistos: {valores}")
    else:
        print(
            "❌ externalStatus NÃO existe neste payload — a detecção de "
            "não-entrega está INERTE. Use a lista acima pra achar o campo certo."
        )
    print(
        f"detectadas como não entregues pela regra atual: "
        f"{len(mensagens_nao_entregues(out))}"
    )


if __name__ == "__main__":
    asyncio.run(main())
