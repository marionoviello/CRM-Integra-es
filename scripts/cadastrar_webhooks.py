#!/usr/bin/env python
"""Cadastra os webhooks (ZapSign + Asaas) que avisam o funil do pós-fechamento.

Uso (no VPS, com o .env preenchido):
    uv run python scripts/cadastrar_webhooks.py --dry-run   # só mostra os alvos
    uv run python scripts/cadastrar_webhooks.py             # cadastra os 2
    uv run python scripts/cadastrar_webhooks.py --so-zapsign
    uv run python scripts/cadastrar_webhooks.py --so-asaas

Lê TUDO do .env. Cadastra:
  - ZapSign → {FUNIL_BASE_URL}/webhooks/zapsign  (evento doc_signed; header
    secreto X-Zapsign-Secret = ZAPSIGN_WEBHOOK_SECRET — a ZapSign não assina
    com HMAC, validamos esse header constant-time ao receber)
  - Asaas   → {FUNIL_BASE_URL}/webhooks/asaas     (PAYMENT_CONFIRMED + RECEIVED;
    authToken = ASAAS_WEBHOOK_TOKEN, devolvido no header asaas-access-token)

Rodar UMA vez por ambiente (primeiro sandbox; depois, na virada, produção).
Re-rodar DUPLICA o webhook no painel — se precisar refazer, apague o antigo
no painel da ZapSign/Asaas antes. Pré-requisito: os endpoints precisam estar
no ar (deploy feito, CONTRATOS_* = true), senão o webhook cadastra mas as
entregas dão 404 até o deploy.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from noviello_funil.asaas import AsaasClient
from noviello_funil.config import Settings
from noviello_funil.zapsign_client import ZapSignClient


async def _cadastrar(
    settings: Settings, *, zapsign: bool, asaas: bool, dry_run: bool,
) -> int:
    base = settings.funil_base_url.rstrip("/")
    rc = 0

    if zapsign:
        url = f"{base}/webhooks/zapsign"
        if not (settings.contratos_zapsign and settings.zapsign_api_token):
            print("PULADO ZapSign: CONTRATOS_ZAPSIGN/ZAPSIGN_API_TOKEN ausentes")
        elif not settings.zapsign_webhook_secret:
            print("ERRO ZapSign: ZAPSIGN_WEBHOOK_SECRET vazio "
                  "(sem ele o webhook fica sem autenticação)", file=sys.stderr)
            rc = 2
        elif dry_run:
            print(f"[dry-run] ZapSign → {url}  "
                  "evento=doc_signed  header=X-Zapsign-Secret:***")
        else:
            client = ZapSignClient(
                settings.zapsign_api_token, settings.zapsign_base_url,
            )
            try:
                await client.register_webhook(
                    url=url,
                    event_type="doc_signed",
                    secret_value=settings.zapsign_webhook_secret,
                )
                print(f"OK ZapSign → {url}")
            finally:
                await client.aclose()

    if asaas:
        url = f"{base}/webhooks/asaas"
        email = settings.contrato_escritorio_email or settings.smtp_user
        if not (settings.contratos_asaas and settings.asaas_api_key):
            print("PULADO Asaas: CONTRATOS_ASAAS/ASAAS_API_KEY ausentes")
        elif not settings.asaas_webhook_token:
            print("ERRO Asaas: ASAAS_WEBHOOK_TOKEN vazio", file=sys.stderr)
            rc = 2
        elif not email:
            print("ERRO Asaas: sem email pra aviso de falha "
                  "(preencha CONTRATO_ESCRITORIO_EMAIL ou SMTP_USER)",
                  file=sys.stderr)
            rc = 2
        elif dry_run:
            print(f"[dry-run] Asaas → {url}  "
                  f"eventos=PAYMENT_CONFIRMED,PAYMENT_RECEIVED  email={email}")
        else:
            client = AsaasClient(
                settings.asaas_api_key, settings.asaas_base_url,
                user_agent=settings.asaas_user_agent,
            )
            try:
                r = await client.register_webhook(
                    url=url,
                    auth_token=settings.asaas_webhook_token,
                    email=email,
                )
                print(f"OK Asaas → {url}  (id: {r.get('id')})")
            finally:
                await client.aclose()

    return rc


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cadastra os webhooks do funil (ZapSign + Asaas).",
    )
    ap.add_argument("--so-zapsign", action="store_true",
                    help="cadastra só o webhook da ZapSign")
    ap.add_argument("--so-asaas", action="store_true",
                    help="cadastra só o webhook do Asaas")
    ap.add_argument("--dry-run", action="store_true",
                    help="mostra os alvos sem cadastrar nada")
    args = ap.parse_args()

    settings = Settings()
    if not settings.funil_base_url:
        print("ERRO: FUNIL_BASE_URL vazio no .env "
              "(URL pública do funil, ex.: https://funil.noviello.adv.br)",
              file=sys.stderr)
        return 2

    so_um = args.so_zapsign or args.so_asaas
    return asyncio.run(_cadastrar(
        settings,
        zapsign=args.so_zapsign or not so_um,
        asaas=args.so_asaas or not so_um,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    raise SystemExit(main())
