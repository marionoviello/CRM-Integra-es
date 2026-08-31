"""Teste da escrita do vínculo de área via API (31/ago) — v2: sub-rotas.

O PATCH direto (``/conversation/{id}`` body ``{"legalAreaId": ...}``, o
mesmo do painel) devolveu 200 'ok' mas NÃO aplicou — o painel seguiu
"Nenhuma área vinculada" (200 falso, igual ao caso do archive em 10/jul,
que se resolveu com a sub-rota /archive). Agora que o DevTools nos deu o
nome exato do recurso (legalArea), esta v2 varre as sub-rotas candidatas
na conversa "Mario eu" (reversível). 404 = rota não existe; 2xx = SUSPEITO
de ter funcionado (conferir no painel).
"""

import asyncio

import httpx

from noviello_funil.config import Settings

AREA_DIREITO_DA_SAUDE = "cmptwamgp0023wly34mr437kv"
CONVERSA_MARIO_EU = "cmryyxpwx00vlql0ixlyneefl"

_BODY = {"legalAreaId": AREA_DIREITO_DA_SAUDE}
_TENTATIVAS = (
    ("GET", "legalArea", None),
    ("GET", "legal-area", None),
    ("PATCH", "legalArea", _BODY),
    ("PATCH", "legal-area", _BODY),
    ("PATCH", "legalarea", _BODY),
    ("PATCH", "area", _BODY),
    ("PUT", "legalArea", _BODY),
    ("POST", "legalArea", _BODY),
)


async def main() -> None:
    s = Settings()
    async with httpx.AsyncClient(
        base_url=s.jurichat_base_url,
        headers={"x-jurichat-api-key": s.jurichat_api_key},
        timeout=20,
    ) as c:
        suspeitas = []
        for metodo, sufixo, body in _TENTATIVAS:
            rota = f"/conversation/{CONVERSA_MARIO_EU}/{sufixo}"
            r = await c.request(metodo, rota, json=body)
            corpo = r.text[:120].replace("\n", " ")
            print(f"{metodo:5} .../{sufixo:12} -> {r.status_code} {corpo}")
            if metodo != "GET" and r.status_code < 400:
                suspeitas.append(f"{metodo} {rota}")
        if suspeitas:
            print("\n2xx de escrita em:", "; ".join(suspeitas))
            print("Confira no painel se a Área apareceu em 'Mario eu'.")
        else:
            print("\nNenhuma sub-rota de escrita existe — a chave de API não "
                  "tem como vincular área. Vou propor o plano B.")


if __name__ == "__main__":
    asyncio.run(main())
