#!/usr/bin/env python
"""Registra no bot uma reunião criada POR FORA (ex: agendada à mão no Calendar),
pra o motor de lembretes (24h/2h/30min) cuidar dela.

Uso (no VPS):
    .venv/bin/python scripts/registrar_reuniao_manual.py \
        --conversa <jurichat_conversation_id> \
        --quando 2026-06-17T10:00:00-03:00 \
        --meet https://meet.google.com/xxx-xxxx-xxx \
        [--event-id <google_calendar_event_id>]

Acha o lead pela conversa e chama ``set_reuniao``, que PRÉ-MARCA como "enviado"
os lembretes cuja janela já passou. Ex.: reunião a menos de 24h → o lembrete de
24h NÃO dispara (fica pré-marcado); só o de 2h e o de 30min disparam, no horário.
Não mexe no ``estado`` do lead (se está aguardando_humano, continua).

``--quando`` deve ser ISO 8601 COM offset (ex.: -03:00 = Brasília). Requer o
.env do funil (DATABASE_PATH). Só atualiza o lead — não envia nada agora.
"""

from __future__ import annotations

import argparse
import sys

from noviello_funil.config import Settings
from noviello_funil.db import connect
from noviello_funil.state import get_lead_by_conversation, set_reuniao


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Registra reunião manual no motor de lembretes do bot.",
    )
    ap.add_argument("--conversa", required=True,
                    help="jurichat_conversation_id do lead")
    ap.add_argument("--quando", required=True,
                    help="ISO 8601 com offset, ex 2026-06-17T10:00:00-03:00")
    ap.add_argument("--meet", default="", help="link do Google Meet")
    ap.add_argument("--event-id", default="",
                    help="id do evento no Google Calendar (opcional)")
    args = ap.parse_args()

    settings = Settings()
    conn = connect(settings.database_path)
    try:
        lead = get_lead_by_conversation(conn, args.conversa)
        if lead is None:
            print(f"ERRO: nenhum lead com conversa={args.conversa}",
                  file=sys.stderr)
            return 2
        set_reuniao(
            conn, lead["id"],
            reuniao_em_iso=args.quando,
            event_id=args.event_id,
            meet_link=args.meet,
        )
        conn.commit()
        print(f"OK: reunião registrada no lead {lead['id']} "
              f"({lead['contato_nome'] or '?'}) para {args.quando}")
        print("Lembretes de 2h e 30min ficam a cargo do reminder_cycle; "
              "o de 24h é pré-marcado como enviado (reunião <24h).")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
