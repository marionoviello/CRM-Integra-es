"""Triagem financeira da carteira — dinheiro a levantar e constrição (2.10).

Varre as movimentações dos processos (fonte: DataJud/CNJ) atrás de dois
tipos de evento que valem dinheiro e exigem ação rápida — em sentidos
opostos:

  💰 *Levantar*  — há valor do cliente pra receber: RPV, precatório,
                   alvará, levantamento/liberação de depósito. Agir = sacar.
  ⚠️ *Constrição* — o cliente vai PERDER bem/dinheiro: penhora, bloqueio,
                   arresto, indisponibilidade, leilão. Agir = defender
                   (embargos, impugnação, prazo correndo).

O Juridiq lista andamentos, mas não separa "isto é dinheiro" do ruído. Este
job faz a triagem e manda só o que importa, ao canal INTERNO (nunca ao
lead). É só screening — a mensagem pede confirmação no painel antes de agir
(ex.: "levantamento de penhora" cai em constrição mas é o oposto; o humano
decide).

Idempotente: um evento é um FATO datado — alerta 1 vez e nunca mais. A
identidade é (processo + dataHora completo + código da TPU), NÃO o texto
livre do movimento (que o CNJ reemite com variações). Crucial: os eventos
só são marcados como "vistos" DEPOIS do envio bem-sucedido — uma falha de
envio não pode enterrar um alerta financeiro pra sempre (a tabela é
insert-only, não se autocorrige como a do carteira_datajud).

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
from collections import Counter, defaultdict
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

MAX_LISTA = 25     # cap de itens exibidos por categoria (o resto reaparece)
LIMITE_MSG = 3500  # teto por mensagem, com margem sob o ~4096 do WhatsApp

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
# LEVANTAR captura tanto a EXPEDIÇÃO (alvará/RPV emitido) quanto a
# LIBERAÇÃO (dinheiro de fato saindo do depósito) — em muitos autos só
# aparece "Liberação de Depósito" ou "Conversão de Depósito em Renda",
# sem a palavra "levantamento". "pagamento" cru fica de fora (vira falso
# positivo de custas/honorários).
_LEVANTAR = [
    r"\bexpedi\w*\s+(de\s+)?rpv\b",
    r"\brequisi\w+\s+de\s+pequeno\s+valor\b",
    r"\bprecat[óo]rio\b",
    r"\balvar[áa]\b",
    r"\blevantament\w+\s+(de\s+)?(valor|dep[óo]sito|quantia|import[âa]ncia)",
    r"\blibera[çc][ãa]o\s+(de\s+)?(dep[óo]sito|valor|quantia|import[âa]ncia)",
    r"\bconvers[ãa]o\s+de\s+dep[óo]sito\s+em\s+renda\b",
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


def _norm_nome(nome: object) -> str:
    """Normaliza texto p/ usar como chave estável (lower + colapsa espaços)."""
    return re.sub(r"\s+", " ", str(nome or "").strip().lower())


def evento_hash(processo: str, data_hora: str, chave_evento: str) -> str:
    """Identidade estável do evento p/ idempotência.

    ``chave_evento`` é o CÓDIGO da TPU (estável) quando existe, senão o nome
    normalizado. ``data_hora`` é o timestamp COMPLETO (não o dia) — assim
    dois eventos distintos no mesmo dia não colapsam, e o mesmo evento
    reemitido com texto diferente não duplica.
    """
    bruto = f"{processo}|{data_hora}|{chave_evento}".encode()
    return hashlib.sha1(bruto).hexdigest()


def eventos_financeiros(
    processo: dict, hoje: datetime.date, janela_dias: int,
) -> list[dict]:
    """Eventos financeiros de UM processo dentro da janela de tempo.

    Espera ``processo['movimentos']`` = [{'nome', 'data', 'codigo'}] (já
    consultado no DataJud). Ignora movimento sem data, fora da janela ou
    não-financeiro.

    Nota de fuso: ``hoje`` está em BRT e ``data`` vem em UTC do DataJud; o
    corte compara o dia-calendário UTC. Skew máximo de 1 dia na BORDA
    inferior, sempre por inclusão (pega ~3h a mais de eventos antigos),
    nunca excluindo evento recente. Mesma tolerância já em produção no
    carteira_datajud — aceita de propósito (screening).
    """
    corte = hoje - datetime.timedelta(days=janela_dias)
    numero = processo.get("processNumber") or "(sem número)"
    resp = _responsavel(processo)
    achados = []
    for mov in processo.get("movimentos") or []:
        data_hora = str(mov.get("data") or "")
        d = _data(data_hora)
        if d is None or d < corte:
            continue
        tipo = classificar_movimento(mov.get("nome"))
        if tipo is None:
            continue
        nome = str(mov.get("nome") or "").strip()
        codigo = mov.get("codigo")
        chave = str(codigo) if codigo not in (None, "") else _norm_nome(nome)
        achados.append({
            "processo": numero,
            "responsavel": resp,
            "tipo": tipo,
            "nome": nome,
            "data": data_hora[:10],   # dia, p/ exibição
            "hash": evento_hash(numero, data_hora, chave),
        })
    return achados


def calcular_novos(conn, eventos: list[dict]) -> set[str]:
    """Quais hashes ainda NÃO foram alertados. Só LÊ (não grava).

    A gravação (marcar_vistos) só acontece DEPOIS do envio bem-sucedido —
    senão uma falha de envio enterraria o alerta pra sempre (insert-only).
    """
    novos: set[str] = set()
    for ev in eventos:
        h = ev["hash"]
        if h in novos:
            continue
        ja = conn.execute(
            "SELECT 1 FROM triagem_financeira_visto WHERE evento_hash = ?", (h,),
        ).fetchone()
        if not ja:
            novos.add(h)
    return novos


def marcar_vistos(conn, eventos: list[dict], hashes: set[str]) -> None:
    """Grava os hashes JÁ ALERTADOS (após envio OK), tudo-ou-nada.

    Recebe só os hashes que de fato entraram na mensagem enviada — os que
    foram truncados ficam de fora e reaparecem na próxima rodada (sem
    perda silenciosa).
    """
    if not hashes:
        return
    por_hash = {e["hash"]: e for e in eventos}
    conn.execute("BEGIN IMMEDIATE")
    try:
        for h in hashes:
            e = por_hash.get(h) or {}
            conn.execute(
                "INSERT OR IGNORE INTO triagem_financeira_visto "
                "(evento_hash, processo, tipo) VALUES (?, ?, ?)",
                (h, e.get("processo", ""), e.get("tipo")),
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def montar_mensagem(
    eventos: list[dict], novos: set[str],
) -> tuple[str | None, set[str]]:
    """Mensagem WhatsApp-ready só com os eventos NOVOS.

    Retorna ``(texto, incluidos)`` — ``incluidos`` são os hashes que de
    fato entraram no texto (≤ MAX_LISTA por categoria). Só esses devem ser
    marcados como vistos; os truncados reaparecem depois. ``(None, set())``
    se não há nada novo.
    """
    pendentes = [e for e in eventos if e["hash"] in novos]
    if not pendentes:
        return None, set()

    por_tipo: dict[str, list[dict]] = defaultdict(list)
    for e in pendentes:
        por_tipo[e["tipo"]].append(e)

    blocos = [
        "💰 *Triagem financeira da carteira*",
        "\nMovimentações que mexem com dinheiro (fonte: DataJud/CNJ). "
        "Confira no painel antes de agir:",
    ]
    incluidos: set[str] = set()
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
            incluidos.add(e["hash"])
        if len(itens) > MAX_LISTA:
            blocos.append(
                f"… e mais {len(itens) - MAX_LISTA} (chegam na próxima rodada)."
            )

    blocos.append(
        "\nTriagem automática — *desbloqueio/levantamento de penhora* podem "
        "cair como constrição; confirme o sentido nos autos."
    )
    return "\n".join(blocos), incluidos


def fatiar_mensagem(texto: str, limite: int = LIMITE_MSG) -> list[str]:
    """Quebra a mensagem em pedaços <= ``limite``, sempre em quebra de linha.

    Preserva bullets inteiros (não corta no meio). Uma linha sozinha maior
    que o limite (raríssimo) vai como seu próprio bloco em vez de travar.
    """
    blocos: list[str] = []
    atual = ""
    for ln in texto.split("\n"):
        if atual and len(atual) + 1 + len(ln) > limite:
            blocos.append(atual)
            atual = ln
        else:
            atual = ln if not atual else f"{atual}\n{ln}"
    if atual:
        blocos.append(atual)
    return blocos


def consultar_movimentos_datajud(
    client: httpx.Client, process_number: str, api_key: str,
) -> tuple[list[dict], str]:
    """Movimentações do processo no DataJud → ([{nome, data, codigo}], status).

    Espelha o consultar_datajud do carteira_datajud, mas devolve os TEXTOS
    e o código da TPU (não só a data mais recente). Erro por processo não
    derruba a varredura.
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
            {
                "nome": mov.get("nome"),
                "data": mov.get("dataHora"),
                "codigo": mov.get("codigo"),
            }
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
    from noviello_funil.outbound import JurichatClient, OutboundError

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
    # Contabiliza quem foi PULADO (sem número, tribunal não mapeado, erro)
    # pra não dar falsa sensação de cobertura total.
    dj = httpx.Client(timeout=30.0)
    limiter = _RateLimiter(RATE_MIN_INTERVALO)
    progresso = {"n": 0}
    cobertura: Counter = Counter()
    plock = threading.Lock()

    def _consultar(p: dict) -> None:
        num = p.get("processNumber") or ""
        st = "sem_numero"
        if num:
            limiter.aguardar()
            p["movimentos"], st = consultar_movimentos_datajud(
                dj, num, settings.datajud_api_key
            )
        with plock:
            cobertura[st] += 1
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

    pulados = {k: v for k, v in cobertura.items() if k != "ok"}
    if pulados:
        logger.warning(
            "triagem_financeira: cobertura parcial — %d ok, pulados: %s",
            cobertura.get("ok", 0), dict(pulados),
        )

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
        novos = calcular_novos(conn, eventos)
        texto, incluidos = montar_mensagem(eventos, novos)
        if texto is None:
            return 0
        logger.info("triagem_financeira:\n%s", texto)

        async def _enviar() -> int:
            """Envia (fatiado) a cada destino. Retorna nº de destinos que
            receberam TUDO. send_message LEVANTA em falha — ao contrário do
            notify_mario, aqui a gente PRECISA saber se chegou."""
            jurichat = JurichatClient(
                api_key=settings.jurichat_api_key,
                base_url=settings.jurichat_base_url,
                bot_user_id=settings.jurichat_bot_user_id,
            )
            destinos = [
                c.strip() for c in settings.mario_conversation_id.split(",") if c.strip()
            ]
            blocos = fatiar_mensagem(texto)
            ok_total = 0
            try:
                for conv in destinos:
                    ok = True
                    for bloco in blocos:
                        try:
                            await jurichat.send_message(conv, bloco)
                        except (
                            OutboundError, httpx.HTTPStatusError, httpx.RequestError,
                        ) as exc:
                            logger.error(
                                "triagem_financeira: envio falhou (%s): %s", conv, exc,
                            )
                            ok = False
                            break
                    if ok:
                        ok_total += 1
            finally:
                await jurichat.aclose()
            return ok_total

        enviados = asyncio.run(_enviar())
        if enviados:
            # Só agora marca como vistos — e só o que foi exibido/enviado.
            marcar_vistos(conn, eventos, incluidos)
        else:
            logger.error(
                "triagem_financeira: 0 destinos receberam — NÃO marca vistos, "
                "re-tenta na próxima rodada (alerta financeiro não pode sumir)"
            )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
