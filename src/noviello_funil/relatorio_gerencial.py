"""Relatório gerencial semanal — o pulso da operação (roadmap 2.8 + 2.9).

Os outros jobs dão os DETALHES acionáveis (saúde da carteira, tarefas
vencendo, publicações). Este dá a VISÃO MACRO em 30 segundos: tamanho e
valor da carteira, saúde do monitoramento e carga de trabalho por
responsável. É o "como vai o escritório" semanal.

Dados reais (jun/2026): 284 processos (quase todos do Mario — a quebra
por responsável vive nas TAREFAS, onde a Hilde aparece); valor da causa
preenchido em ~64%; sem tags de vertical (não dá pra quebrar por área).

Read-only, agregado, sem nome de cliente no corpo (só contagens/valores).

Execução: console script ``noviello-gerencial`` via timer semanal
(segunda 07h BRT — antes do expediente, junto dos outros de segunda).
"""

import asyncio
import datetime
import logging
import re
from collections import Counter

import httpx

from noviello_funil.tarefas import _listar_tarefas, classificar_tarefas

logger = logging.getLogger(__name__)


def parse_valor_brl(s: object) -> float:
    """'R$ 35.180,19' → 35180.19. Não parseável → 0.0."""
    txt = str(s or "")
    txt = re.sub(r"[^\d,.]", "", txt)        # tira R$, espaços (incl. \xa0)
    if not txt:
        return 0.0
    txt = txt.replace(".", "").replace(",", ".")  # milhar BR → decimal
    try:
        return float(txt)
    except ValueError:
        return 0.0


def resumir_carteira(processos: list[dict]) -> dict:
    """Agrega a carteira: total, segredo, valor sob gestão, monitoramento."""
    total = len(processos)
    em_segredo = sum(1 for p in processos if p.get("isSecret"))
    valor_total = sum(parse_valor_brl(p.get("valueOfCause")) for p in processos)
    monit = Counter(
        (p.get("monitoringStatus") or "—") for p in processos
    )
    return {
        "total": total,
        "em_segredo": em_segredo,
        "valor_total": round(valor_total, 2),
        "monitoramento": dict(monit),
    }


def _fmt_brl(v: float) -> str:
    # 1234567.89 → "1.234.567,89"
    inteiro, _, dec = f"{v:.2f}".partition(".")
    milhar = re.sub(r"(?<=\d)(?=(\d{3})+$)", ".", inteiro)
    return f"{milhar},{dec}"


def montar_relatorio(carteira: dict, tarefas_resumo: dict, hoje: datetime.date) -> str:
    """Relatório gerencial WhatsApp-ready. Sempre envia (pulso semanal)."""
    t = carteira["total"]
    plural = "processos ativos" if t != 1 else "processo ativo"
    blocos = [
        f"📊 *Relatório gerencial* (semana de {hoje.day:02d}/{hoje.month:02d})",
        f"\n🗂️ *Carteira*: {t} {plural}"
        + (f", {carteira['em_segredo']} em segredo" if carteira["em_segredo"] else ""),
        f"💰 Valor sob gestão: R$ {_fmt_brl(carteira['valor_total'])}",
    ]

    m = carteira["monitoramento"]
    erro = m.get("ERRO", 0)
    ok = m.get("CADASTRADO", 0)
    blocos.append(
        f"🩺 Monitoramento: {ok} ativos"
        + (f", ⚠️ {erro} com erro" if erro else "")
        + (f", {m.get('SEGREDO', 0)} em segredo" if m.get("SEGREDO") else "")
    )

    abertas = tarefas_resumo.get("abertas", 0)
    vencidas = tarefas_resumo.get("vencidas", 0)
    blocos.append(
        f"\n📋 *Tarefas*: {abertas} abertas"
        + (f", 🔴 {vencidas} vencidas" if vencidas else "")
    )
    por_resp = tarefas_resumo.get("por_responsavel", {})
    for resp, qtd in sorted(por_resp.items(), key=lambda x: -x[1]):
        blocos.append(f"• {resp}: {qtd}")

    blocos.append("\nVisão macro — os detalhes vão nos alertas específicos.")
    return "\n".join(blocos)


def _listar_processos(client: httpx.Client) -> list[dict]:
    procs, page = [], 1
    while True:
        r = client.get("/lawSuit/", params={"page": page, "limit": 100})
        r.raise_for_status()
        data = r.json()
        procs.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1
    return procs


def _resumir_tarefas(tarefas: list[dict], hoje: datetime.date) -> dict:
    """Conta tarefas abertas/vencidas e a carga por responsável.

    "Aberta" = não concluída e não arquivada (mesma regra do tarefas.py).
    """
    diag = classificar_tarefas(tarefas, hoje)
    por_resp: Counter = Counter()
    abertas = 0
    for t in tarefas:
        col = t.get("column")
        nome_col = col.get("name") if isinstance(col, dict) else col
        if nome_col == "Concluída" or t.get("isArchived"):
            continue
        abertas += 1
        for r in (t.get("responsibles") or []):
            if r.get("name"):
                por_resp[r["name"]] += 1
    return {
        "abertas": abertas,
        "vencidas": len(diag["vencidas"]),
        "por_responsavel": dict(por_resp),
    }


def main() -> int:
    """Entry point do console script ``noviello-gerencial``."""
    from zoneinfo import ZoneInfo

    from noviello_funil.config import Settings
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("gerencial: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("gerencial: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        processos = _listar_processos(client)
        tarefas = _listar_tarefas(client)
    finally:
        client.close()

    hoje = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    carteira = resumir_carteira(processos)
    tarefas_resumo = _resumir_tarefas(tarefas, hoje)
    texto = montar_relatorio(carteira, tarefas_resumo, hoje)
    logger.info("gerencial:\n%s", texto)

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
