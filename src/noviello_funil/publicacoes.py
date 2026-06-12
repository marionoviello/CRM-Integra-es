"""Alerta diário de publicações não tratadas no Juridiq.

Publicação de diário oficial parada sem tratamento = risco de prazo
perdido. O Juridiq marca cada publicação com ``isHandled``; este job
varre as não tratadas e manda a lista no WhatsApp do Mario (canal de
alertas). Tratou no painel → alerta para sozinho no dia seguinte.

Execução: console script ``noviello-publicacoes`` via systemd timer
diário (08h30 BRT, meia hora após o de aniversários). Zero não
tratadas → não envia nada (sem ruído). Enquanto houver pendência, o
alerta REPETE todo dia — nag intencional, prazo é sério.

Nota: diferente do GET /person/ (filtros quebrados), o filtro
``isHandled`` do GET /publication/ FUNCIONA (verificado 2026-06-11
contra a base real: 99 publicações, isHandled=false retornou só 1).
"""

import asyncio
import logging
import re

import httpx

logger = logging.getLogger(__name__)

# Cap de itens detalhados na mensagem (o total sempre aparece no topo).
MAX_ITENS = 10
_RESUMO_CHARS = 90

# processNumber quando o Juridiq não identifica o processo na publicação.
_SEM_PROCESSO = ("", "não encontrado", "nao encontrado")


def _data_curta(raw: object) -> str:
    """'11/06/2026' ou '2026-06-11[...]' → '11/06'. Vazio → '?'."""
    s = str(raw or "").strip()
    if not s:
        return "?"
    m = re.match(r"^(\d{2})/(\d{2})/\d{4}", s)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    m = re.match(r"^\d{4}-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(2)}/{m.group(1)}"
    return s


def _data_ordenavel(raw: object) -> str:
    """Chave de ordenação ISO-like; datas não parseáveis vão pro fim."""
    s = str(raw or "").strip()
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    return "9999-99-99"


def buscar_nao_tratadas(client: httpx.Client) -> list[dict]:
    """GET /publication/?isHandled=false paginado, normalizado.

    Cada item: {id, processo, resumo, data, diario}.
    """
    brutas, page = [], 1
    while True:
        resp = client.get(
            "/publication/",
            params={"page": page, "limit": 100, "isHandled": "false"},
        )
        resp.raise_for_status()
        data = resp.json()
        brutas.extend(data.get("data", []))
        if page >= int(data.get("totalPages") or 1):
            break
        page += 1

    pubs = []
    for p in brutas:
        processo = str(p.get("processNumber") or "").strip()
        if processo.lower() in _SEM_PROCESSO:
            processo = ""
        diario = (p.get("officialDiary") or "").strip()
        # title traz o tipo do ato ("Nova citação"); descriptionSmall
        # costuma só repetir o nome do diário (verificado 2026-06-11) —
        # candidato igual ao diário é descartado.
        resumo = next(
            (
                c.strip()
                for c in (p.get("title"), p.get("descriptionSmall"))
                if c and c.strip() and c.strip() != diario
            ),
            "",
        )
        pubs.append({
            "id": p.get("id") or "",
            "processo": processo,
            "resumo": resumo,
            "data": p.get("publicationDate") or p.get("availabilityDate"),
            "diario": diario,
        })
    return pubs


def montar_mensagem(pubs: list[dict]) -> str:
    """Mensagem WhatsApp-ready pro canal de alertas do Mario."""
    n = len(pubs)
    plural = "publicações não tratadas" if n > 1 else "publicação não tratada"
    cab = f"📌 *{n} {plural} no Juridiq*\n"

    ordenadas = sorted(pubs, key=lambda p: _data_ordenavel(p.get("data")))
    linhas = []
    for p in ordenadas[:MAX_ITENS]:
        ref = p["processo"] or p["diario"] or "(sem referência)"
        resumo = p["resumo"][:_RESUMO_CHARS]
        if len(p["resumo"]) > _RESUMO_CHARS:
            resumo += "…"
        linha = f"• {_data_curta(p.get('data'))} — {ref}"
        if resumo:
            linha += f"\n   _{resumo}_"
        linhas.append(linha)

    extra = ""
    if n > MAX_ITENS:
        extra = f"\n… e mais {n - MAX_ITENS}.\n"
    rodape = (
        "\nTrate no painel do Juridiq (Publicações) pra silenciar "
        "este alerta."
    )
    return cab + "\n" + "\n".join(linhas) + "\n" + extra + rodape


def main() -> int:
    """Entry point do console script ``noviello-publicacoes``.

    Zero publicações não tratadas → exit 0 silencioso (nenhum envio).
    """
    from noviello_funil.config import Settings
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("publicacoes: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("publicacoes: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        pubs = buscar_nao_tratadas(client)
    finally:
        client.close()

    logger.info("publicacoes: %d não tratada(s)", len(pubs))
    if not pubs:
        return 0

    texto = montar_mensagem(pubs)
    logger.info("publicacoes:\n%s", texto)

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
