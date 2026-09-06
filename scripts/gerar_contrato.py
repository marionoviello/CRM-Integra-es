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
import contextlib
import datetime
import json
import re
import sys
from typing import Any

from noviello_funil.asaas import AsaasClient
from noviello_funil.config import Settings
from noviello_funil.contrato import parse_templates_por_tipo, template_do_tipo
from noviello_funil.db import connect, run_migrations
from noviello_funil.orquestrador_contrato import (
    args_politica,
    gerar_contrato,
    montar_signers_padrao,
)
from noviello_funil.zapsign_client import ZapSignClient

# Estados ABERTOS: o orquestrador RETOMA esses sozinho (dedupe por
# external_reference — não cria 2ª cobrança nem 2º contrato). Bloquear aqui
# seria uma regressão de comportamento pra TODO tipo de caso, não só os
# automáticos: rodar o script de novo sobre um contrato aberto é exatamente
# o caminho normal de retry, e é seguro.
_ESTADOS_ABERTOS_RETOMAVEIS: tuple[str, ...] = (
    "contrato_montagem", "contrato_criando_doc", "contrato_pendente_revisao",
)

# Estados VIVOS-FECHADOS: já existe um contrato de verdade em curso com o
# cliente — liberado (ele já pode assinar) ou assinado. Rodar de novo AQUI é
# o cenário perigoso: cria um 2º contrato e uma 2ª cobrança REAL no Asaas
# (e, sob política automática, libera essa 2ª assinatura também). Só esses
# estados bloqueiam e exigem --novo-caso.
_ESTADOS_VIVOS_FECHADOS: tuple[str, ...] = (
    "contrato_liberado", "contrato_assinado",
)
# Qualquer outro estado (contrato_reprovado, contrato_recusado,
# contrato_expirado, e o que mais existir) não é nem retomável nem vivo: o
# contrato anterior morreu sem chegar a nada com o cliente. Um contrato novo
# aqui não é duplicata de coisa nenhuma — só um aviso informativo, sem freio.


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
    # Console do Windows não é UTF-8 por padrão: sem isto, o aviso da guarda
    # de duplicata (que o operador precisa ler sob pressão) vira mojibake
    # ("j� existe(m)", "cobran�a"). suppress porque reconfigure não existe em
    # todo stream (ex.: saída capturada em teste).
    for _stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            _stream.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(
        description="Gera um contrato pelo pipeline (caminho A).",
    )
    ap.add_argument(
        "input",
        help=(
            "JSON com tipo_caso, cliente, honorarios e, opcionalmente, "
            "template_id (senão vem de ZAPSIGN_TEMPLATE_POR_TIPO)"
        ),
    )
    ap.add_argument(
        "--novo-caso", action="store_true",
        help=(
            "Confirma que este é OUTRO caso genuinamente diferente do mesmo "
            "cliente (ex.: outro voo) — não uma retentativa do mesmo caso. "
            "Só é exigida quando já existe um contrato VIVO (liberado ao "
            "cliente ou assinado) para o mesmo CPF + tipo de caso; um "
            "contrato aberto é retomado automaticamente, sem precisar desta "
            "flag."
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
    # Modelo ZapSign: o JSON pode trazer template_id explícito; se não
    # trouxer, vem do .env por tipo de caso. Sem os dois = erro alto (nunca
    # um modelo "default" silencioso — seria contrato errado pro caso errado).
    if not dados.get("template_id"):
        dados["template_id"] = template_do_tipo(
            dados["tipo_caso"],
            parse_templates_por_tipo(settings.zapsign_template_por_tipo),
        )
        if not dados["template_id"]:
            print(
                f"ERRO: sem template_id no JSON e sem entrada para "
                f"tipo_caso {dados['tipo_caso']!r} em ZAPSIGN_TEMPLATE_POR_TIPO",
                file=sys.stderr,
            )
            return 2

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        # Guarda de duplicata: separa contrato ABERTO (o orquestrador retoma
        # sozinho, sem 2ª cobrança — deixar passar) de contrato VIVO-FECHADO
        # (liberado/assinado — rodar de novo cria 2º contrato + 2ª cobrança
        # REAL, aí sim bloqueia). Qual é o critério que separa "mesmo caso"
        # de "caso novo" (ex.: 2 voos do mesmo cliente) é decisão do Mario,
        # não deste script — por isso exige --novo-caso só no caso vivo.
        cpf_digitos = re.sub(r"\D", "", str(dados["cliente"].get("cpf", "")))
        tipo_caso = dados["tipo_caso"]
        existentes = conn.execute(
            "SELECT id, estado, criado_em FROM contrato "
            "WHERE cpf = ? AND tipo_caso = ? ORDER BY id DESC",
            (cpf_digitos, tipo_caso),
        ).fetchall()
        abertos = [r for r in existentes if r["estado"] in _ESTADOS_ABERTOS_RETOMAVEIS]
        vivos_fechados = [r for r in existentes if r["estado"] in _ESTADOS_VIVOS_FECHADOS]
        outros = [
            r for r in existentes
            if r["estado"] not in _ESTADOS_ABERTOS_RETOMAVEIS
            and r["estado"] not in _ESTADOS_VIVOS_FECHADOS
        ]

        for row in abertos:
            print(
                f">>> contrato {row['id']} (estado={row['estado']}) já aberto "
                f"para CPF {cpf_digitos} + tipo_caso {tipo_caso} — será "
                "RETOMADO; nenhuma cobrança nova será criada.",
                file=sys.stderr,
            )
        for row in outros:
            print(
                f">>> contrato {row['id']} anterior (estado={row['estado']}, "
                f"criado_em={row['criado_em']}) para CPF {cpf_digitos} + "
                f"tipo_caso {tipo_caso} não seguiu adiante — não é "
                "duplicata, prosseguindo.",
                file=sys.stderr,
            )

        if vivos_fechados and not args.novo_caso:
            print(
                f"ERRO: já existe(m) {len(vivos_fechados)} contrato(s) VIVO(S) "
                f"para CPF {cpf_digitos} + tipo_caso {tipo_caso}:",
                file=sys.stderr,
            )
            for row in vivos_fechados:
                print(
                    f"  - contrato {row['id']}: estado={row['estado']} "
                    f"criado_em={row['criado_em']}",
                    file=sys.stderr,
                )
            print(
                "Rodar de novo cria um SEGUNDO contrato e uma SEGUNDA "
                "cobrança REAL no Asaas. Se este é de fato um caso novo do "
                "mesmo cliente (ex.: outro voo), rode de novo com "
                "--novo-caso.",
                file=sys.stderr,
            )
            return 1
        if vivos_fechados and args.novo_caso:
            print(
                f">>> --novo-caso: criando mais um contrato para CPF "
                f"{cpf_digitos} + tipo_caso {tipo_caso} (já existe(m) "
                f"{len(vivos_fechados)} vivo(s)) — nova cobrança real será "
                "gerada."
            )
        resultado = asyncio.run(_run(settings, conn, dados))
    finally:
        conn.close()

    print(json.dumps(resultado, ensure_ascii=False, indent=2))
    if resultado.get("status") in ("pendente_revisao", "em_andamento"):
        print()
        print(">>> Revise o PDF e APROVE aqui:", resultado.get("link_aprovacao"))
        print(">>> Link de pagamento (cliente):", resultado.get("invoice_url"))
    elif resultado.get("status") == "liberado_automatico":
        print()
        print(
            ">>> LIBERADO AUTOMATICAMENTE — sem revisão humana (contrato "
            f"{resultado.get('contrato_id')})."
        )
        print(">>> Link de assinatura (cliente):", resultado.get("sign_url"))
        print(">>> Link de pagamento (cliente):", resultado.get("invoice_url"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
