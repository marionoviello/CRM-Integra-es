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

from noviello_funil.config import Settings
from noviello_funil.db import connect, run_migrations
from noviello_funil.webhooks import build_lead_message_processor, register_webhooks


def create_app() -> FastAPI:
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":%(message)r}',
    )

    conn = connect(settings.database_path)
    run_migrations(conn)

    processor = build_lead_message_processor(get_db=lambda: conn)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
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
