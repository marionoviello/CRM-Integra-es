#!/usr/bin/env python
"""Gatilho de terminal: gera um contrato pelo pipeline (caminho A).

Uso:
    uv run python scripts/gerar_contrato.py caso.json

Lê um JSON com ``{tipo_caso, template_id, cliente{...}, honorarios{valor,
valor_extenso, vencimento?}}``, monta os signatários FIXOS (escritório + 2
testemunhas) da config, e roda o orquestrador: conflito → escopo → cobrança
Asaas → doc ZapSign EM SILÊNCIO → devolve o LINK DE APROVAÇÃO. Você revê o PDF
real e libera; NADA vai pro cliente até a sua aprovação — EXCETO nos tipos
de caso marcados como automáticos em CONTRATO_POLITICA_POR_TIPO, em que a
assinatura é liberada na hora (ver politica_contrato.py).

Requer no .env: CONTRATOS_ZAPSIGN/ZAPSIGN_API_TOKEN, CONTRATOS_ASAAS/
ASAAS_API_KEY, FUNIL_BASE_URL, e os signatários (CONTRATO_ESCRITORIO_*,
CONTRATO_TESTEMUNHA_*). Comece no SANDBOX (ASAAS_BASE_URL=api-sandbox...).
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import json
import re
import sys
from typing import Any

from noviello_funil.asaas import AsaasClient
from noviello_funil.config import Settings
from noviello_funil.db import connect, run_migrations
from noviello_funil.orquestrador_contrato import (
    args_politica,
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
            **args_politica(settings),
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
    ap.add_argument(
        "--novo-caso", action="store_true",
        help=(
            "Confirma que este é um caso GENUINAMENTE DIFERENTE do mesmo "
            "cliente (ex.: outro voo), mesmo já havendo contrato para este "
            "CPF + tipo de caso. Sem esta flag, o script recusa criar um "
            "2º contrato/2ª cobrança por engano."
        ),
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
        # Guarda de duplicata: o índice único (uq_contrato_aberto) só cobre
        # estados ABERTOS. Um contrato já LIBERADO/ASSINADO sai do campo de
        # visão do dedupe — uma 2ª rodada deste script pro mesmo CPF+tipo_caso
        # criaria um contrato novo, uma 2ª cobrança REAL no Asaas e, sob
        # política automática, liberaria essa 2ª assinatura também. Qual é o
        # critério que separa "mesmo caso" de "caso novo" (ex.: 2 voos do
        # mesmo cliente) é decisão do Mario, não deste script — por isso o
        # script recusa decidir sozinho e exige --novo-caso.
        cpf_digitos = re.sub(r"\D", "", str(dados["cliente"].get("cpf", "")))
        tipo_caso = dados["tipo_caso"]
        existentes = conn.execute(
            "SELECT id, estado, criado_em FROM contrato "
            "WHERE cpf = ? AND tipo_caso = ? ORDER BY id DESC",
            (cpf_digitos, tipo_caso),
        ).fetchall()
        if existentes and not args.novo_caso:
            print(
                f"ERRO: já existe(m) {len(existentes)} contrato(s) para "
                f"CPF {cpf_digitos} + tipo_caso {tipo_caso}:",
                file=sys.stderr,
            )
            for row in existentes:
                print(
                    f"  - contrato {row['id']}: estado={row['estado']} "
                    f"criado_em={row['criado_em']}",
                    file=sys.stderr,
                )
            print(
                "Rodar de novo cria um SEGUNDO contrato e uma SEGUNDA "
                "cobrança REAL no Asaas — e, se a política do tipo de caso "
                "for automática, libera essa segunda assinatura ao cliente "
                "também. Se este é de fato um caso novo do mesmo cliente "
                "(ex.: outro voo), rode de novo com --novo-caso.",
                file=sys.stderr,
            )
            return 1
        if existentes and args.novo_caso:
            print(
                f">>> --novo-caso: criando mais um contrato para CPF "
                f"{cpf_digitos} + tipo_caso {tipo_caso} (já existem "
                f"{len(existentes)}) — nova cobrança real será gerada."
            )
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
