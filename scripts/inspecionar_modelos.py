#!/usr/bin/env python
"""Lista os modelos do ZapSign e confere as variáveis contra o pipeline.

Uso (no VPS):
    .venv/bin/python scripts/inspecionar_modelos.py

Pra cada modelo cadastrado na sua conta ZapSign, imprime o ``template_id``
(token) e compara os ``{{placeholders}}`` do modelo com os 23 que o pipeline
injeta (montar_data_contrato). Mostra o que FALTA no modelo (sairia em branco)
e o que é EXTRA (placeholder do modelo que o pipeline não preenche). Assim você
acha o template_id certo e confirma o casamento ANTES de gerar o 1º contrato.

Só LÊ (GET) — não cria nem altera nada. Requer ZAPSIGN_API_TOKEN no .env.
"""

from __future__ import annotations

import asyncio
import sys

from noviello_funil.config import Settings
from noviello_funil.contrato import _VARS_CLIENTE, _VARS_ESCOPO
from noviello_funil.zapsign_client import ZapSignClient

# As 23 variáveis que o pipeline preenche (cliente + escopo + os 3 valores).
ESPERADAS: set[str] = (
    set(_VARS_CLIENTE)
    | set(_VARS_ESCOPO)
    | {"{{VALOR_HONORARIOS}}", "{{VALOR_HONORARIOS_EXTENSO}}", "{{LINK_PAGAMENTO}}"}
)


async def _run(settings: Settings) -> None:
    zapsign = ZapSignClient(settings.zapsign_api_token, settings.zapsign_base_url)
    try:
        templates = await zapsign.list_templates()
        if not templates:
            print("Nenhum modelo encontrado na conta ZapSign.")
            return
        print(f"{len(templates)} modelo(s) na conta ZapSign:")
        for t in templates:
            token = t.get("token") or ""
            nome = t.get("name") or "(sem nome)"
            det = await zapsign.get_template(token)
            do_modelo = {
                i.get("variable")
                for i in det.get("inputs", [])
                if i.get("variable")
            }
            faltam = ESPERADAS - do_modelo
            extras = do_modelo - ESPERADAS
            print(f"\n=== {nome} ===")
            print(f"template_id: {token}")
            if not faltam and not extras:
                print("  ✓ as 23 variáveis casam 100% com o pipeline")
            else:
                if faltam:
                    print(f"  ⚠ FALTAM no modelo ({len(faltam)}) — sairiam em branco:")
                    print(f"      {', '.join(sorted(faltam))}")
                if extras:
                    print(f"  ℹ extras no modelo ({len(extras)}) — o pipeline não preenche:")
                    print(f"      {', '.join(sorted(extras))}")
    finally:
        await zapsign.aclose()


def main() -> int:
    settings = Settings()
    if not settings.zapsign_api_token:
        print("ERRO: ZAPSIGN_API_TOKEN ausente no .env", file=sys.stderr)
        return 2
    asyncio.run(_run(settings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
