"""FastAPI app factory + wiring.

Composition root: this is the ONE place that imports every other module
and connects them. Everywhere else uses dependency injection.
"""

import logging
from contextlib import asynccontextmanager
from functools import partial

from anthropic import AsyncAnthropic
from fastapi import FastAPI

from noviello_funil.brain import load_skill, triagem
from noviello_funil.config import Settings
from noviello_funil.db import connect, run_migrations
from noviello_funil.outbound import JurichatClient
from noviello_funil.webhooks import build_lead_message_processor, register_webhooks


def create_app() -> FastAPI:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
    )

    conn = connect(settings.database_path)
    run_migrations(conn)

    jurichat = JurichatClient(
        api_key=settings.jurichat_api_key,
        base_url=settings.jurichat_base_url,
    )
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    skill = load_skill("saude_suplementar")

    bound_triagem = partial(
        triagem,
        client=anthropic_client,
        model=settings.anthropic_model,
        skill_content=skill,
    )

    processor = build_lead_message_processor(
        get_db=lambda: conn,
        jurichat=jurichat,
        mario_conversation_id=settings.mario_conversation_id,
        triagem_fn=bound_triagem,
        max_turnos=settings.max_turnos_por_lead,
        followup_horas=settings.followup_1_apos_horas,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        await jurichat.aclose()
        conn.close()

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

    return app


app = create_app()
