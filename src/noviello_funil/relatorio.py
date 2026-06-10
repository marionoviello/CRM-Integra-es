"""Relatório semanal de funil — métricas dos últimos 7 dias via WhatsApp.

Fonte de dados: tabela ``transicoes`` (cada mudança de estado registra
motivo + timestamp desde o dia 1) + snapshot da tabela ``leads``.

Execução: console script ``noviello-relatorio`` disparado por systemd
timer toda segunda 11:00 UTC (08h BRT). Sem estado de controle — o
timer com ``Persistent=true`` garante exatamente 1 disparo por semana.
"""

import asyncio
import datetime
import logging
import sqlite3

logger = logging.getLogger(__name__)

# Motivos de transição que contam como "handoff pra equipe" (lead saiu
# do bot pra atendimento humano por decisão do Claude ou guardrail).
_MOTIVOS_HANDOFF = (
    "claude_propor",
    "claude_handoff",
    "max_turnos",
    "calendar_nao_configurado",
    "agenda_lotada_proximos_dias",
    "falha_criar_evento_calendar",
)


def _count_transicoes(
    conn: sqlite3.Connection, motivos: tuple[str, ...], dias: int,
) -> int:
    placeholders = ",".join("?" for _ in motivos)
    row = conn.execute(
        f"""SELECT COUNT(*) AS n FROM transicoes
            WHERE motivo IN ({placeholders})
              AND criado_em >= datetime('now', ?)""",
        (*motivos, f"-{dias} days"),
    ).fetchone()
    return row["n"]


def gerar_relatorio_semanal(
    conn: sqlite3.Connection, *, dias: int = 7,
) -> str:
    """Monta o texto do relatório (WhatsApp-ready, sem HTML)."""
    novos = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE criado_em >= datetime('now', ?)",
        (f"-{dias} days",),
    ).fetchone()["n"]

    agendamentos = _count_transicoes(
        conn, ("claude_confirmar_horario",), dias,
    )
    handoffs = _count_transicoes(conn, _MOTIVOS_HANDOFF, dias)
    followups = _count_transicoes(
        conn, ("scheduler_followup_1", "scheduler_followup_2"), dias,
    )
    encerrados = _count_transicoes(
        conn, ("scheduler_encerramento",), dias,
    )
    em_conversa = conn.execute(
        "SELECT COUNT(*) AS n FROM leads WHERE estado = 'em_conversa'",
    ).fetchone()["n"]

    taxa = f" ({agendamentos * 100 // novos}% dos novos)" if novos else ""

    hoje = datetime.date.today()
    inicio = hoje - datetime.timedelta(days=dias)
    meses = [
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    ]
    periodo = (
        f"{inicio.day:02d}/{meses[inicio.month - 1]} – "
        f"{hoje.day:02d}/{meses[hoje.month - 1]}"
    )

    return (
        f"📊 *Relatório semanal — Funil Noviello*\n"
        f"Semana {periodo}\n\n"
        f"• Leads novos: {novos}\n"
        f"• Agendamentos criados: {agendamentos}{taxa}\n"
        f"• Handoffs pra equipe: {handoffs}\n"
        f"• Follow-ups enviados: {followups}\n"
        f"• Encerrados sem resposta: {encerrados}\n"
        f"• Em conversa agora: {em_conversa}\n\n"
        f"Gerado automaticamente pelo bot."
    )


def main() -> int:
    """Entry point do console script ``noviello-relatorio``.

    Monta o relatório e envia pro canal de alertas do Mario via
    notify_mario (fire-and-forget). Exit 0 mesmo se o envio falhar —
    o relatório não é crítico o bastante pra alarmar o systemd.
    """
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("relatorio: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        texto = gerar_relatorio_semanal(conn)
    finally:
        conn.close()

    logger.info("relatorio semanal:\n%s", texto)

    async def _send() -> None:
        jurichat = JurichatClient(
            api_key=settings.jurichat_api_key,
            base_url=settings.jurichat_base_url,
        )
        try:
            await notify_mario(
                jurichat,
                mario_conversation_id=settings.mario_conversation_id,
                mensagem=texto,
            )
        finally:
            await jurichat.aclose()

    asyncio.run(_send())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
