"""Agenda + SLA de tarefas — cartão matinal de prazos (roadmap 1.8 + 1.9).

O Juridiq cria tarefas (inclusive as "Auto Tarefas - Movimentação" com
prazo) mas não empurra um resumo de quem está vencendo. Este job lê o
kanban (GET /task/), separa as tarefas ABERTAS em vencidas e vencendo
nos próximos dias, agrupa por responsável e manda um cartão matinal ao
Mario. É o "segundo par de olhos" sobre prazos — pressão saudável de
fechamento sem caçar card por card.

Sem audiências cadastradas no Juridiq (/audience/ vazio em jun/2026), a
"agenda do dia" e o "SLA de tarefas" do roadmap convergem nas tarefas
com prazo — que é o dado real disponível.

Cobertura depende da disciplina de cadastrar prazo como tarefa (as
auto-tarefas já cobrem as movimentações). Tudo aberto e não concluído.

Execução: console script ``noviello-tarefas`` via timer diário (07h30
BRT, antes do expediente). Nada vencido/vencendo → silêncio.
"""

import asyncio
import datetime
import logging

import httpx

logger = logging.getLogger(__name__)

JANELA_VENCENDO_DIAS = 3   # "vencendo" = prazo nos próximos N dias
MAX_POR_SECAO = 15


def _data(s: object) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def _coluna(t: dict) -> str:
    c = t.get("column")
    return c.get("name", "") if isinstance(c, dict) else (c or "")


def _responsavel(t: dict) -> str:
    nomes = [r.get("name", "") for r in (t.get("responsibles") or []) if r.get("name")]
    return ", ".join(nomes) or "sem responsável"


def classificar_tarefas(tasks: list[dict], hoje: datetime.date) -> dict:
    """Separa as tarefas ABERTAS em {vencidas, vencendo}.

    Aberta = não está na coluna "Concluída" e não arquivada.
    Vencida = finalDate < hoje. Vencendo = hoje..hoje+JANELA.
    """
    limite = hoje + datetime.timedelta(days=JANELA_VENCENDO_DIAS)
    vencidas, vencendo = [], []
    for t in tasks:
        if _coluna(t) == "Concluída" or t.get("isArchived"):
            continue
        d = _data(t.get("finalDate"))
        if d is None:
            continue
        item = {
            "title": (t.get("title") or "(sem título)").strip(),
            "responsavel": _responsavel(t),
            "finalDate": d.isoformat(),
            "processo": (t.get("processNumber") or "").strip(),
        }
        if d < hoje:
            item["dias_atraso"] = (hoje - d).days
            vencidas.append(item)
        elif d <= limite:
            vencendo.append(item)
    vencidas.sort(key=lambda x: x["finalDate"])       # mais antiga (atrasada) 1º
    vencendo.sort(key=lambda x: x["finalDate"])       # mais próxima 1º
    return {"vencidas": vencidas, "vencendo": vencendo}


def _por_responsavel(itens: list[dict]) -> dict[str, list[dict]]:
    grupos: dict[str, list[dict]] = {}
    for it in itens:
        grupos.setdefault(it["responsavel"], []).append(it)
    return grupos


def montar_mensagem(diag: dict, hoje: datetime.date) -> str | None:
    """Cartão matinal WhatsApp-ready, ou None se nada vencido/vencendo."""
    vencidas, vencendo = diag["vencidas"], diag["vencendo"]
    if not vencidas and not vencendo:
        return None

    blocos = [f"📋 *Tarefas com prazo* (hoje, {hoje.day:02d}/{hoje.month:02d})"]

    if vencidas:
        blocos.append(f"\n🔴 *{len(vencidas)} VENCIDA(s)*:")
        for resp, itens in _por_responsavel(vencidas[:MAX_POR_SECAO]).items():
            blocos.append(f"_{resp}_:")
            for it in itens:
                ref = f" — {it['processo']}" if it["processo"] else ""
                blocos.append(
                    f"• {it['title']}{ref} "
                    f"(venceu {it['finalDate']}, há {it['dias_atraso']}d)"
                )
        if len(vencidas) > MAX_POR_SECAO:
            blocos.append(f"… e mais {len(vencidas) - MAX_POR_SECAO}.")

    if vencendo:
        blocos.append(f"\n🟡 *{len(vencendo)} vencendo até {JANELA_VENCENDO_DIAS}d*:")
        for resp, itens in _por_responsavel(vencendo[:MAX_POR_SECAO]).items():
            blocos.append(f"_{resp}_:")
            for it in itens:
                ref = f" — {it['processo']}" if it["processo"] else ""
                blocos.append(f"• {it['title']}{ref} (prazo {it['finalDate']})")
        if len(vencendo) > MAX_POR_SECAO:
            blocos.append(f"… e mais {len(vencendo) - MAX_POR_SECAO}.")

    blocos.append("\nFonte: kanban do Juridiq. Conferir/baixar no painel.")
    return "\n".join(blocos)


def _listar_tarefas(client: httpx.Client) -> list[dict]:
    tarefas, page = [], 1
    while True:
        r = client.get("/task/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        tarefas.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return tarefas


def main() -> int:
    """Entry point do console script ``noviello-tarefas``.

    Nada vencido/vencendo → exit 0 silencioso.
    """
    from zoneinfo import ZoneInfo

    from noviello_funil.config import Settings
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("tarefas: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("tarefas: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        tarefas = _listar_tarefas(client)
    finally:
        client.close()

    hoje = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    diag = classificar_tarefas(tarefas, hoje)
    logger.info(
        "tarefas: %d vencidas, %d vencendo (de %d)",
        len(diag["vencidas"]), len(diag["vencendo"]), len(tarefas),
    )

    texto = montar_mensagem(diag, hoje)
    if texto is None:
        return 0
    logger.info("tarefas:\n%s", texto)

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
