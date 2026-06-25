"""FastAPI app factory + wiring.

Composition root: this is the ONE place that imports every other module
and connects them. Everywhere else uses dependency injection.

After the polling refactor (R01), the FastAPI app is responsible only for:
  * receiving webhooks (HMAC + idempotency + register-and-wake)
  * serving /health

All Claude-driven work lives in the scheduler process (systemd timer
firing `noviello-followup` every minute). The two processes share the
SQLite database.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from noviello_funil.asaas import AsaasClient
from noviello_funil.config import Settings
from noviello_funil.db import connect, run_migrations
from noviello_funil.juridiq_client import JuridiqClient
from noviello_funil.outbound import JurichatClient
from noviello_funil.rotas_contrato import register_contrato_routes
from noviello_funil.webhooks import build_lead_message_processor, register_webhooks
from noviello_funil.zapsign_client import ZapSignClient


def create_app() -> FastAPI:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
    )

    conn = connect(settings.database_path)
    run_migrations(conn)

    processor = build_lead_message_processor(get_db=lambda: conn)

    # Clientes do pipeline de contrato — só instanciados quando a feature está
    # ligada E o segredo existe (feature opcional; sem flag, fica None e as
    # rotas respondem de forma segura). JurichatClient é reusado pra notificar
    # o Mario (assinatura/cobrança).
    zapsign: ZapSignClient | None = None
    if settings.contratos_zapsign and settings.zapsign_api_token:
        zapsign = ZapSignClient(
            settings.zapsign_api_token, settings.zapsign_base_url,
        )
    asaas: AsaasClient | None = None
    if settings.contratos_asaas and settings.asaas_api_key:
        asaas = AsaasClient(
            settings.asaas_api_key, settings.asaas_base_url,
            user_agent=settings.asaas_user_agent,
        )
    # #36 (25/jun): pós-assinatura ZapSign — intake/tarefa no Juridiq. Só
    # instancia com a flag ligada E a chave presente (default off, sandbox-first).
    juridiq: JuridiqClient | None = None
    if settings.pos_assinatura_ativo and settings.juridiq_api_key:
        juridiq = JuridiqClient(
            settings.juridiq_api_key, settings.juridiq_base_url,
        )
    jurichat: JurichatClient | None = None
    if zapsign is not None or asaas is not None or juridiq is not None:
        jurichat = JurichatClient(
            settings.jurichat_api_key, settings.jurichat_base_url,
            bot_user_id=settings.jurichat_bot_user_id,
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        conn.close()
        for cliente in (zapsign, asaas, juridiq, jurichat):
            if cliente is not None:
                await cliente.aclose()

    app = FastAPI(title="Noviello Funil Saúde", lifespan=lifespan)

    @app.get("/health")
    async def health():
        return {"ok": True}

    register_webhooks(
        app,
        get_db=lambda: conn,
        webhook_secret=settings.jurichat_webhook_secret,
        process_lead_message=processor,
    )

    register_contrato_routes(
        app,
        get_db=lambda: conn,
        settings=settings,
        zapsign=zapsign,
        asaas=asaas,
        jurichat=jurichat,
        juridiq=juridiq,
    )

    return app


app = create_app()
