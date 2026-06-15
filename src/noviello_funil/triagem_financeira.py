"""Triagem financeira da carteira — dinheiro a levantar e constrição (2.10).

Varre as movimentações dos processos (fonte: DataJud/CNJ) atrás de dois
tipos de evento que valem dinheiro e exigem ação rápida — em sentidos
opostos:

  💰 *Levantar*  — há valor do cliente pra receber: RPV, precatório,
                   alvará, levantamento de depósito. Agir = sacar.
  ⚠️ *Constrição* — o cliente vai PERDER bem/dinheiro: penhora, bloqueio,
                   arresto, indisponibilidade, leilão. Agir = defender
                   (embargos, impugnação, prazo correndo).

O Juridiq lista andamentos, mas não separa "isto é dinheiro" do ruído. Este
job faz a triagem e manda só o que importa, ao canal INTERNO (nunca ao
lead). É só screening — a mensagem pede confirmação no painel antes de agir
(ex.: "levantamento de penhora" cai em constrição mas é o oposto; o humano
decide).

Idempotente: um evento é um FATO datado (processo+data+nome) — alerta 1 vez
e nunca mais. A janela só limita quão pra trás a 1ª rodada olha.

Reaproveita a infra de consulta paralela ao DataJud do ``carteira_datajud``
(rate limiter + alias dos tribunais). Execução: console script
``noviello-triagem-financeira`` via systemd timer semanal.

API DataJud: chave PÚBLICA oficial do CNJ — não é segredo.
"""

import datetime
import hashlib
import logging
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import httpx

from noviello_funil.carteira_datajud import (
    CONCORRENCIA,
    RATE_MIN_INTERVALO,
    _alias_datajud,
    _data,
    _listar_processos,
    _RateLimiter,
    _responsavel,
    _so_digitos,
)

logger = logging.getLogger(__name__)

MAX_LISTA = 25  # cap de itens por categoria na mensagem

# Léxico dos eventos financeiros no texto da movimentação (DataJud/CNJ).
# Tudo em minúsculo, casado com re.search sobre o nome do movimento.
#
# CONSTRIÇÃO primeiro (mais urgente — prazo pra defender). Termos
# escolhidos pra alta precisão: "desbloqueio" não casa "\bbloqueio\b"
# (sem boundary antes do 'b'); evitamos "praça" solto (vira endereço).
_CONSTRICAO = [
    r"\bpenhora\b",
    r"\barresto\b",
    r"\bsequestro\b",
    r"\bbloqueio\b",
    r"\bsisbajud\b",
    r"\bbacenjud\b",
    r"\bindisponibilidade\b",
    r"\bleil[ãa]o\b",
    r"\bhasta\s+p[úu]blica\b",
    r"\barremata\w+",
    r"\badjudica\w+",
    r"\baliena[çc][ãa]o\s+judicial\b",
]
_LEVANTAR = [
    r"\bexpedi\w*\s+(de\s+)?rpv\b",
    r"\brequisi\w+\s+de\s+pequeno\s+valor\b",
    r"\bprecat[óo]rio\b",
    r"\balvar[áa]\b",
    r"\blevantament\w+\s+(de\s+)?(valor|dep[óo]sito|quantia|import[âa]ncia)",
]
_RX_CONSTRICAO = [re.compile(p) for p in _CONSTRICAO]
_RX_LEVANTAR = [re.compile(p) for p in _LEVANTAR]

_ROTULO = {"constricao": "⚠️ Constrição", "levantar": "💰 Levantar"}


def classificar_movimento(nome: object) -> str | None:
    """'penhora'→'constricao', 'Expedição de RPV'→'levantar', ruído→None.

    Constrição tem prioridade (checada antes) — se um texto raro casar os
    dois, o risco vence.
    """
    txt = str(nome or "").lower()
    if any(rx.search(txt) for rx in _RX_CONSTRICAO):
        return "constricao"
    if any(rx.search(txt) for rx in _RX_LEVANTAR):
        return "levantar"
    return None


def evento_hash(processo: str, data_iso: str, nome: str) -> str:
    """Identidade estável do evento p/ idempotência (processo+data+nome)."""
    bruto = f"{processo}|{data_iso}|{nome}".encode()
    return hashlib.sha1(bruto).hexdigest()


def eventos_financeiros(
    processo: dict, hoje: datetime.date, janela_dias: int,
) -> list[dict]:
    """Eventos financeiros de UM processo dentro da janela de tempo.

    Espera ``processo['movimentos']`` = [{'nome', 'data'}] (já consultado
    no DataJud). Ignora movimento sem data, fora da janela ou não-financeiro.
    """
    corte = hoje - datetime.timedelta(days=janela_dias)
    numero = processo.get("processNumber") or "(sem número)"
    resp = _responsavel(processo)
    achados = []
    for mov in processo.get("movimentos") or []:
        d = _data(mov.get("data"))
        if d is None or d < corte:
            continue
        tipo = classificar_movimento(mov.get("nome"))
        if tipo is None:
            continue
        data_iso = str(mov.get("data") or "")[:10]
        nome = str(mov.get("nome") or "").strip()
        achados.append({
            "processo": numero,
            "responsavel": resp,
            "tipo": tipo,
            "nome": nome,
            "data": data_iso,
            "hash": evento_hash(numero, data_iso, nome),
        })
    return achados


def diff_novos(conn, eventos: list[dict]) -> set[str]:
    """Quais hashes ainda não tinham sido alertados. Só insere (nunca remove).

    Um evento financeiro é um fato datado: depois de alertado, não volta.
    """
    novos: set[str] = set()
    for ev in eventos:
        h = ev["hash"]
        if h in novos:
            continue
        ja = conn.execute(
            "SELECT 1 FROM triagem_financeira_visto WHERE evento_hash = ?", (h,),
        ).fetchone()
        if ja:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO triagem_financeira_visto "
            "(evento_hash, processo, tipo) VALUES (?, ?, ?)",
            (h, ev["processo"], ev["tipo"]),
        )
        novos.add(h)
    return novos


def montar_mensagem(eventos: list[dict], novos: set[str]) -> str | None:
    """Mensagem WhatsApp-ready só com os eventos NOVOS. None se não há."""
    pendentes = [e for e in eventos if e["hash"] in novos]
    if not pendentes:
        return None

    por_tipo: dict[str, list[dict]] = defaultdict(list)
    for e in pendentes:
        por_tipo[e["tipo"]].append(e)

    blocos = [
        "💰 *Triagem financeira da carteira*",
        "\nMovimentações que mexem com dinheiro (fonte: DataJud/CNJ). "
        "Confira no painel antes de agir:",
    ]
    # Constrição primeiro (prazo pra defender), depois oportunidade.
    for tipo in ("constricao", "levantar"):
        itens = por_tipo.get(tipo)
        if not itens:
            continue
        itens.sort(key=lambda e: e["data"], reverse=True)
        blocos.append(f"\n*{_ROTULO[tipo]}* ({len(itens)})")
        for e in itens[:MAX_LISTA]:
            blocos.append(
                f"• {e['processo']} — {e['responsavel']} "
                f"({e['data']}): {e['nome']}"
            )
        if len(itens) > MAX_LISTA:
            blocos.append(f"… e mais {len(itens) - MAX_LISTA}.")

    blocos.append(
        "\nTriagem automática — *desbloqueio/levantamento de penhora* podem "
        "cair como constrição; confirme o sentido no autos."
    )
    return "\n".join(blocos)


def consultar_movimentos_datajud(
    client: httpx.Client, process_number: str, api_key: str,
) -> tuple[list[dict], str]:
    """Movimentações do processo no DataJud → ([{nome, data}], status).

    Espelha o consultar_datajud do carteira_datajud, mas devolve os TEXTOS
    (não só a data mais recente). Erro por processo não derruba a varredura.
    """
    alias = _alias_datajud(process_number)
    if not alias:
        return [], "tribunal_nao_mapeado"
    try:
        r = client.post(
            f"https://api-publica.datajud.cnj.jus.br/api_publica_{alias}/_search",
            headers={
                "Authorization": f"APIKey {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": {"match": {"numeroProcesso": _so_digitos(process_number)}},
                "size": 10,
            },
        )
        if r.status_code >= 400:
            return [], f"http_{r.status_code}"
        hits = r.json().get("hits", {}).get("hits", [])
        movs = [
            {"nome": mov.get("nome"), "data": mov.get("dataHora")}
            for h in hits
            for mov in (h.get("_source") or {}).get("movimentos", []) or []
            if mov.get("dataHora")
        ]
        return movs, "ok"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        return [], f"erro_{type(exc).__name__}"


def main() -> int:
    """Entry point do console script ``noviello-triagem-financeira``.

    Sem evento financeiro novo → exit 0 silencioso (nenhum envio).
    """
    import asyncio
    from zoneinfo import ZoneInfo

    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("triagem_financeira: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning(
            "triagem_financeira: MARIO_CONVERSATION_ID não configurado — pulando"
        )
        return 0

    jq = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        processos = _listar_processos(jq)
    finally:
        jq.close()

    # Anexa as movimentações do DataJud a cada processo, EM PARALELO, com o
    # mesmo rate limiter global do carteira_datajud (sob a cota do CNJ).
    dj = httpx.Client(timeout=30.0)
    limiter = _RateLimiter(RATE_MIN_INTERVALO)
    progresso = {"n": 0}
    plock = threading.Lock()

    def _consultar(p: dict) -> None:
        num = p.get("processNumber") or ""
        if num:
            limiter.aguardar()
            p["movimentos"], _ = consultar_movimentos_datajud(
                dj, num, settings.datajud_api_key
            )
        with plock:
            progresso["n"] += 1
            if progresso["n"] % 50 == 0:
                logger.info(
                    "triagem_financeira: %d/%d consultados",
                    progresso["n"], len(processos),
                )

    try:
        with ThreadPoolExecutor(max_workers=CONCORRENCIA) as ex:
            list(ex.map(_consultar, processos))
    finally:
        dj.close()

    hoje = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    eventos = [
        ev
        for p in processos
        for ev in eventos_financeiros(p, hoje, settings.triagem_financeira_janela_dias)
    ]
    logger.info(
        "triagem_financeira: %d eventos financeiros na janela (de %d processos)",
        len(eventos), len(processos),
    )

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        novos = diff_novos(conn, eventos)
    finally:
        conn.close()

    texto = montar_mensagem(eventos, novos)
    if texto is None:
        return 0
    logger.info("triagem_financeira:\n%s", texto)

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
