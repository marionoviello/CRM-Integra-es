"""Boletim diário do andamento da integração AASP → Juridiq.

Pedido do Mario em 25/08 (semana de estreia da integração): um retrato
consolidado no fim do dia — quantas intimações entraram, quantas viraram
andamento/tarefa, acumulado — pra acompanhar a adaptação SEM depender de
juntar as mensagens avulsas dos jobs. Manda TODO dia dentro da janela
(mesmo com zero intimações: heartbeat que prova que a malha rodou).

Janela: até ``aasp_boletim_ate`` (default 2026-08-30, sexta da semana de
estreia). Depois disso o job silencia sozinho (exit 0) — pra manter,
basta esvaziar/estender a variável no .env.

Fonte: só o SQLite local (aasp_intimacao_vista + tarefa_publicacao
"aasp:%") — sem chamada externa; os detalhes por intimação continuam nas
mensagens dos jobs aasp/conferência.

Execução: console script ``noviello-aasp-boletim`` via timer diário
21:00 UTC = 18:00 BRT (depois da conferência das 15h15).
"""

import datetime
import logging

logger = logging.getLogger(__name__)

_MAX_PROCESSOS = 10


def dentro_da_janela(ate: str, hoje: datetime.date) -> bool:
    """True se o boletim ainda deve rodar. Vazio/inválido = sempre roda."""
    s = (ate or "").strip()
    if not s:
        return True
    try:
        limite = datetime.date.fromisoformat(s)
    except ValueError:
        return True
    return hoje <= limite


def coletar(conn) -> dict:
    """Números do dia (UTC — às 18h BRT a data UTC ainda é a mesma) e
    acumulado desde o início da integração."""
    hoje_rows = conn.execute(
        "SELECT processo, law_suit_id FROM aasp_intimacao_vista "
        "WHERE date(criado_em) = date('now')",
    ).fetchall()
    tarefas = conn.execute(
        "SELECT COUNT(*) FROM tarefa_publicacao "
        "WHERE publication_id LIKE 'aasp:%' AND date(criada_em) = date('now')",
    ).fetchone()[0]
    acumulado = conn.execute(
        "SELECT COUNT(*) FROM aasp_intimacao_vista",
    ).fetchone()[0]
    andamentos = [r for r in hoje_rows if (r["law_suit_id"] or "").strip()]
    return {
        "hoje_total": len(hoje_rows),
        "hoje_andamentos": len(andamentos),
        "hoje_fora": len(hoje_rows) - len(andamentos),
        "hoje_tarefas": int(tarefas),
        "acumulado": int(acumulado),
        "hoje_processos": [r["processo"] for r in hoje_rows if r["processo"]],
    }


def montar_boletim(d: dict, hoje: datetime.date) -> str:
    """Mensagem WhatsApp do boletim. Sempre retorna texto (heartbeat)."""
    cab = f"📊 *Boletim AASP — {hoje.strftime('%d/%m')}*"
    if not d["hoje_total"]:
        return (
            f"{cab}\n"
            "Hoje: sem intimação nova no recorte (jobs rodaram normal).\n"
            f"Acumulado desde 24/08: {d['acumulado']} intimação(ões) "
            "processada(s).\n"
            "_Conferência dos e-mails manda mensagem própria às 15h15._"
        )
    linhas = [
        cab,
        f"Hoje: {d['hoje_total']} intimação(ões) nova(s) — "
        f"{d['hoje_andamentos']} viraram andamento [AASP], "
        f"{d['hoje_tarefas']} tarefa(s) de prazo, "
        f"{d['hoje_fora']} fora da carteira.",
    ]
    if d["hoje_processos"]:
        linhas.append("Processos de hoje:")
        for p in d["hoje_processos"][:_MAX_PROCESSOS]:
            linhas.append(f"• {p}")
        if len(d["hoje_processos"]) > _MAX_PROCESSOS:
            linhas.append(f"… e mais {len(d['hoje_processos']) - _MAX_PROCESSOS}.")
    linhas.append(
        f"Acumulado desde 24/08: {d['acumulado']} intimação(ões) processada(s)."
    )
    linhas.append("_Conferência dos e-mails manda mensagem própria às 15h15._")
    return "\n".join(linhas)


def main() -> int:
    """Entry point do console script ``noviello-aasp-boletim``."""
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    hoje = datetime.date.today()
    if not dentro_da_janela(settings.aasp_boletim_ate, hoje):
        logger.info("aasp_boletim: fora da janela (até %s) — silencioso",
                    settings.aasp_boletim_ate)
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("aasp_boletim: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        dados = coletar(conn)
    finally:
        conn.close()

    texto = montar_boletim(dados, hoje)
    logger.info("aasp_boletim:\n%s", texto)

    import asyncio

    async def _send() -> None:
        jurichat = JurichatClient(
            api_key=settings.jurichat_api_key,
            base_url=settings.jurichat_base_url,
            bot_user_id=settings.jurichat_bot_user_id,
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
