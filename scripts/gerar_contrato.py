#!/usr/bin/env python
"""Gatilho de terminal: gera um contrato pelo pipeline (caminho A).

Uso:
    uv run python scripts/gerar_contrato.py caso.json

Lê um JSON com ``{tipo_caso, template_id, cliente{...}, honorarios{valor,
valor_extenso, vencimento?}}``, monta os signatários FIXOS (escritório + 2
testemunhas) da config, e roda o orquestrador: conflito → escopo → cobrança
Asaas → doc ZapSign EM SILÊNCIO → devolve o LINK DE APROVAÇÃO. Você revê o PDF
real e libera; NADA vai pro cliente até a sua aprovação.

Requer no .env: CONTRATOS_ZAPSIGN/ZAPSIGN_API_TOKEN, CONTRATOS_ASAAS/
ASAAS_API_KEY, FUNIL_BASE_URL, e os signatários (CONTRATO_ESCRITORIO_*,
CONTRATO_TESTEMUNHA_*). Comece no SANDBOX (ASAAS_BASE_URL=api-sandbox...).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import sys
from typing import Any

from noviello_funil.asaas import AsaasClient
from noviello_funil.config import Settings
from noviello_funil.db import connect, run_migrations
from noviello_funil.orquestrador_contrato import (
    gerar_contrato,
    montar_signers_padrao,
)
from noviello_funil.zapsign_client import ZapSignClient


async def _run(settings: Settings, conn: Any, dados: dict) -> dict:
    cliente = dados["cliente"]
    honorarios = dados["honorarios"]
    due_date = honorarios.get("vencimento") or (
        datetime.date.today()
        + datetime.timedelta(days=settings.asaas_payment_due_days)
    ).isoformat()

    zapsign = ZapSignClient(settings.zapsign_api_token, settings.zapsign_base_url)
    asaas = AsaasClient(
        settings.asaas_api_key, settings.asaas_base_url,
        user_agent=settings.asaas_user_agent,
    )
    try:
        return await gerar_contrato(
            conn, asaas, zapsign,
            cliente=cliente,
            tipo_caso=dados["tipo_caso"],
            valor_honorarios=float(honorarios["valor"]),
            valor_extenso=honorarios["valor_extenso"],
            template_id=dados["template_id"],
            signers_extra=montar_signers_padrao(settings),
            due_date=due_date,
            base_url=settings.funil_base_url,
        )
    finally:
        await zapsign.aclose()
        await asaas.aclose()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Gera um contrato pelo pipeline (caminho A).",
    )
    ap.add_argument(
        "input", help="JSON com tipo_caso, template_id, cliente, honorarios",
    )
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        dados = json.load(f)

    settings = Settings()
    if not (settings.contratos_zapsign and settings.zapsign_api_token):
        print("ERRO: CONTRATOS_ZAPSIGN/ZAPSIGN_API_TOKEN ausentes no .env",
              file=sys.stderr)
        return 2
    if not (settings.contratos_asaas and settings.asaas_api_key):
        print("ERRO: CONTRATOS_ASAAS/ASAAS_API_KEY ausentes no .env",
              file=sys.stderr)
        return 2

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        resultado = asyncio.run(_run(settings, conn, dados))
    finally:
        conn.close()

    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    if resultado.get("status") in ("pendente_revisao", "ja_em_andamento"):
        print()
        print(">>> Revise o PDF e APROVE aqui:", resultado.get("link_aprovacao"))
        print(">>> Link de pagamento (cliente):", resultado.get("invoice_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
