"""Boletim mensal de andamento ao cliente (roadmap 3.1 — variante mensal).

No ÚLTIMO DIA ÚTIL do mês, resume o andamento dos clientes que tiveram
movimentação no mês e prepara o envio. Decisões do Mario (15/jun):
  - HÍBRIDO: caso claramente seguro → "✅ pronto pra enviar" (mensagem
    pronta no link wa.me); sensível/ambíguo → "⚠️ revisar antes".
  - Processo SEM movimentação no mês → não entra.

v1 (este): tudo vai pro Mario num lote de revisão (notify_mario), com a
classificação ✅/⚠️ e um link wa.me com a mensagem pronta pra ele tocar. É
a 1ª rodada revisável + o mecanismo wa.me que já funciona (aniversários).
Quando o endpoint PROATIVO do Jurichat for confirmado, liga-se o auto-envio
dos ✅ por flag (o JurichatClient atual só envia por conversation_id).

CLASSIFICAÇÃO — WHITELIST, não blacklist (revisão adversarial 15/jun): como
o "auto" vira mensagem PRÉ-PREENCHIDA que o Mario toca sem ler, um falso
negativo seria tom-surdo a quem perdeu casa/ação/liberdade. Então auto SÓ
quando TODO movimento do mês está numa lista pequena de atos procedimentais
neutros E nenhum casa o léxico sensível. Qualquer coisa fora disso →
rascunho (na dúvida, humano). É o gate inverso ao da triagem (que é
blacklist tolerante a falso-negativo, por ser screening interno).

Travas OAB (reuso do 2.4): só telefone autenticado por person_id
(``cliente_processo``); pula segredo; respeita opt-out (1.10); co-autores
e telefone ambíguo/fixo → rascunho; conteúdo sóbrio, "nossa equipe", nunca
"Dr. Mario". Feriados não entram no cálculo do último dia útil (refinamento
antes de ligar o auto-envio). Idempotente por competência (YYYY-MM).
"""

import calendar
import datetime
import logging
import re
import threading
import urllib.parse
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import httpx

from noviello_funil.carteira_datajud import (
    CONCORRENCIA,
    RATE_MIN_INTERVALO,
    _RateLimiter,
)
from noviello_funil.triagem_financeira import (
    _RX_CONSTRICAO,
    _RX_LEVANTAR,
    consultar_movimentos_datajud,
)

logger = logging.getLogger(__name__)

_BRT = ZoneInfo("America/Sao_Paulo")

# WHITELIST: atos procedimentais NEUTROS — os únicos que autorizam a
# mensagem automática. Tudo que não casa aqui cai em rascunho (fail-safe).
_SEGURO = [
    r"\bjuntada\b",
    r"\bconclus[ãa]o\b",
    r"\bconcluso[s]?\b",
    r"\bdespacho\b",
    r"\bpublica[çc][ãa]o\b",
    r"\bvista[s]?\b",
    r"\bremessa\b",
    r"\bredistribui[çc][ãa]o\b",
    r"\bdistribui[çc][ãa]o\b",
    r"\bdecurso\s+de\s+prazo\b",
    r"\bato\s+ordinat[óo]rio\b",
    r"\bmero\s+expediente\b",
    r"\bcertid[ãa]o\b",
    r"\bpeti[çc][ãa]o\b",
    r"\bmanifesta[çc][ãa]o\b",
    r"\brecebimento\b",
    r"\bcarga\b",
    r"\baudi[êe]ncia\b",   # designação/ata — informativo, ok ao cliente
]
_RX_SEGURO = [re.compile(p) for p in _SEGURO]

# BLACKLIST de reforço (defense-in-depth + motivo do rascunho). Reusa
# constrição/dinheiro da triagem e ADICIONA o que o boletim precisa (mais
# abrangente — aqui erra-se a favor do cliente).
_DINHEIRO_EXTRA = [r"\brpv\b", r"\balvar[áa]\b", r"\brequisi[çc]\w+\s+de\s+pequeno\s+valor\b"]
_RX_DINHEIRO = _RX_LEVANTAR + [re.compile(p) for p in _DINHEIRO_EXTRA]

_CONSTRICAO_EXTRA = [
    r"\bsequestro\b",
    r"\bbusca\s+e\s+apreens[ãa]o\b",
    r"\bdespejo\b",
    r"\breintegra[çc][ãa]o\s+de\s+posse\b",
    r"\bimiss[ãa]o\s+(na|de)\s+posse\b",
    r"\bpris[ãa]o\b",
    r"\bconfisco\b",
    r"\bremo[çc][ãa]o\s+de\s+bens\b",
    r"\bavalia[çc][ãa]o\b",
    r"\brecupera[çc][ãa]o\s+judicial\b",
    r"\bfal[êe]ncia\b",
    r"\bpracea\w+",
    r"\bremi[çc][ãa]o\b",
    r"\bpra[çc]a[s]?\b",   # designação de praça (leilão) — solto, no boletim ok
    r"\bhasta\b",
]
_RX_CONSTRICAO_BOLETIM = _RX_CONSTRICAO + [re.compile(p) for p in _CONSTRICAO_EXTRA]

# Desfecho/mérito — casa SUBSTANTIVO e adjetivo (revisão 15/jun: a TPU usa
# 'Improcedência'/'Procedência' substantivo).
_DESFECHO = [
    r"\bsenten[çc]a\b",
    r"\bac[óo]rd[ãa]o\b",
    r"\btr[âa]nsito\s+em\s+julgado\b",
    r"\bimproced[êe]n(te|cia)\b",
    r"\bproced[êe]n(te|cia)\b",
    r"\bjulgad[oa]\b",
    r"\bjulgamento\s+do\s+m[ée]rito\b",
    r"\bextin[çc]\w+",
    r"\barquiv\w+",
    r"\bbaixa\b",
    r"\bhomologa[çc]\w+",
    r"\bliquida[çc][ãa]o\b",
    r"\bdesist[êe]ncia\b",
    r"\bintima[çc][ãa]o\s+\w*\s*pagamento\b",
    # Família INDEFERIMENTO/NEGAÇÃO/INADMISSÃO/RENÚNCIA/SUSPENSÃO (revisão
    # 15/jun): atos adversos que carregam um substantivo da whitelist
    # ('Indeferimento da PETIÇÃO inicial', 'DESPACHO - Negado seguimento') e
    # escapariam pro auto. Na dúvida → rascunho (falso-positivo é barato).
    r"\bindef[ei]r\w*",   # indeferimento/indeferida/indefere/indefiro
    r"\bnegad[oa]\b",
    r"\bnega\w+\s+(seguimento|provimento)\b",
    r"\bn[ãa]o[-\s]provimento\b",
    r"\bdesprovid\w+",
    r"\bimprovid\w+",
    r"\binadmiss\w+",
    r"\binadmitid\w+",
    r"\bn[ãa]o\s+conhec\w+",
    r"\bn[ãa]o\s+interp\w+",   # não interposição/interposto (perdeu o prazo recursal)
    r"\bren[úu]ncia\b",
    r"\bsuspens[ãa]o\b",
    r"\bsobresta\w+",
]
_RX_DESFECHO = [re.compile(p) for p in _DESFECHO]


def ultimo_dia_util_do_mes(d: datetime.date) -> datetime.date:
    """Último dia ÚTIL (seg-sex) do mês de ``d``. Feriados NÃO considerados
    (refinamento antes do auto-envio); o job roda na janela até o fim do mês."""
    ultimo = calendar.monthrange(d.year, d.month)[1]
    dia = datetime.date(d.year, d.month, ultimo)
    while dia.weekday() >= 5:   # 5=sáb, 6=dom
        dia -= datetime.timedelta(days=1)
    return dia


def na_janela_de_envio(hoje: datetime.date) -> bool:
    """True do último dia útil até o fim do mês (margem de retry)."""
    return hoje >= ultimo_dia_util_do_mes(hoje)


def competencia(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _data_brt(iso: object) -> datetime.date | None:
    """Data-calendário em BRT. DataJud manda UTC ('...Z'); convertemos antes
    de comparar com o mês (senão um evento da virada cai no mês errado)."""
    s = str(iso or "")
    if not s:
        return None
    try:
        dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        try:
            return datetime.date.fromisoformat(s[:10])
        except (ValueError, TypeError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(_BRT)
    return dt.date()


def movimentos_do_mes(movimentos: list[dict], ano: int, mes: int) -> list[dict]:
    """Movimentos cuja data (em BRT) cai no mês/ano dado."""
    out = []
    for m in movimentos or []:
        d = _data_brt(m.get("data"))
        if d and d.year == ano and d.month == mes:
            out.append(m)
    return out


def motivo_sensivel(movimentos: list[dict]) -> str | None:
    """Se algum movimento do mês é sensível, devolve o motivo; senão None."""
    for m in movimentos:
        txt = str(m.get("nome") or "").lower()
        if any(rx.search(txt) for rx in _RX_DINHEIRO):
            return "dinheiro (RPV/alvará/levant.)"
        if any(rx.search(txt) for rx in _RX_CONSTRICAO_BOLETIM):
            return "constrição/patrimônio"
        if any(rx.search(txt) for rx in _RX_DESFECHO):
            return "desfecho/decisão"
    return None


def eh_comunicavel_auto(movimentos: list[dict]) -> bool:
    """True só se TODO movimento é procedimental seguro (whitelist) E nenhum
    é sensível. Qualquer movimento desconhecido → False (cai em rascunho)."""
    if not movimentos:
        return False
    if motivo_sensivel(movimentos):
        return False
    return all(
        any(rx.search(str(m.get("nome") or "").lower()) for rx in _RX_SEGURO)
        for m in movimentos
    )


def classificar_boletim(
    movimentos_mes: list[dict],
    *,
    telefone_ambiguo: bool = False,
    multi_cliente: bool = False,
    telefone_movel: bool = True,
) -> dict:
    """skip (sem movimento) | rascunho (qualquer dúvida) | auto (só o seguro).

    Ordem de guardas é proposital: co-autores e telefone primeiro (questões
    de DESTINATÁRIO), depois sensibilidade do conteúdo.
    """
    if not movimentos_mes:
        return {"modo": "skip", "motivo": "sem movimentação no mês"}
    if multi_cliente:
        return {"modo": "rascunho", "motivo": "co-autores — escreva você"}
    if telefone_ambiguo:
        return {"modo": "rascunho", "motivo": "telefone bate com +1 cadastro"}
    if not telefone_movel:
        return {"modo": "rascunho", "motivo": "sem celular no cadastro"}
    mot = motivo_sensivel(movimentos_mes)
    if mot:
        return {"modo": "rascunho", "motivo": mot}
    if eh_comunicavel_auto(movimentos_mes):
        return {"modo": "auto", "motivo": ""}
    return {"modo": "rascunho", "motivo": "movimentação não rotineira"}


def _fmt_data(iso: object) -> str:
    s = str(iso or "")[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def montar_mensagem_cliente(process_number: str, data_ultima_mov: object) -> str:
    """Mensagem sóbria ao cliente (vai no wa.me dos '✅ pronto').

    Fato puro — sem opinião, prognóstico, prazo, nem o texto técnico do
    movimento. "nossa equipe" e saída de opt-out (o 'SAIR' é capturado pelo
    handler de opt-out)."""
    quando = f" em {_fmt_data(data_ultima_mov)}" if data_ultima_mov else " este mês"
    return (
        "Olá! 👋 Passando pra te dar um retorno do seu processo "
        f"nº {process_number}: houve movimentação{quando} e nossa equipe "
        "segue acompanhando de perto. Qualquer dúvida, é só chamar por aqui. 🙏\n"
        "(Se preferir não receber este resumo mensal, responda SAIR.)"
    )


def _e_movel(telefone_chave: str) -> bool:
    """Celular BR: 11 dígitos (DDD + 9 + 8), 3º dígito = 9."""
    d = re.sub(r"\D", "", str(telefone_chave or ""))
    return len(d) == 11 and d[2] == "9"


def melhor_movel(telefones) -> str | None:
    """Um celular do conjunto (p/ wa.me), ou None se só há fixo."""
    moveis = [t for t in telefones if _e_movel(t)]
    return max(moveis, key=len) if moveis else None


def _e164(telefone_chave: str) -> str:
    return "55" + re.sub(r"\D", "", str(telefone_chave or ""))


def wa_me_link(telefone_chave: str, mensagem: str = "") -> str:
    base = f"https://wa.me/{_e164(telefone_chave)}"
    if mensagem:
        return base + "?text=" + urllib.parse.quote(mensagem)
    return base


def montar_lote(itens: list[dict], comp: str, sem_telefone: int = 0) -> str | None:
    """Lote de revisão ao Mario. Sem cap: o fatiamento (fatiar_mensagem)
    pagina tudo, pra nenhum cliente sumir silenciosamente."""
    if not itens:
        return None
    auto = [i for i in itens if i["modo"] == "auto"]
    rasc = [i for i in itens if i["modo"] == "rascunho"]
    blocos = [
        f"🗓️ *Boletim mensal — {comp}*",
        "\nClientes com movimentação no mês. Toque o link pra enviar pelo WhatsApp:",
    ]
    if auto:
        blocos.append(f"\n✅ *Prontos pra enviar* ({len(auto)})")
        for i in auto:
            blocos.append(
                f"• {i['nome']} — proc {i['processo']} "
                f"(mov {_fmt_data(i['data'])}): {i['link']}"
            )
    if rasc:
        blocos.append(f"\n⚠️ *Revisar antes — escreva você* ({len(rasc)})")
        for i in rasc:
            blocos.append(
                f"• {i['nome']} — proc {i['processo']} ({i['motivo']}): {i['link']}"
            )
    rodape = (
        f"\nTotal: {len(itens)} clientes. Pulei sem-movimentação, sigilosos e opt-out."
    )
    if sem_telefone:
        rodape += (
            f" ⚠️ {sem_telefone} com movimentação ficaram de fora por não ter "
            "telefone no cadastro — tratar manualmente."
        )
    blocos.append(rodape)
    return "\n".join(blocos)


def _carregar_clientes(conn) -> dict:
    """cliente_processo → {process_number: {person_ids, is_secret, nome,
    telefones}} + tel_pids (telefone→person_ids, p/ detectar ambiguidade)."""
    procs: dict = {}
    tel_pids: dict = defaultdict(set)
    for pn, pid, secret, nome, tel in conn.execute(
        "SELECT process_number, person_id, is_secret, cliente_nome, telefone_chave "
        "FROM cliente_processo"
    ).fetchall():
        e = procs.setdefault(pn, {
            "person_ids": set(), "is_secret": bool(secret),
            "nomes": {}, "telefones": set(),
        })
        e["person_ids"].add(pid)
        if nome:
            e["nomes"][pid] = nome
        if tel:
            e["telefones"].add(tel)
            tel_pids[tel].add(pid)
    return {"procs": procs, "tel_pids": tel_pids}


def main() -> int:
    """Entry point do console script ``noviello-boletim``."""
    import asyncio

    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.opt_out import esta_suprimido
    from noviello_funil.outbound import JurichatClient, OutboundError
    from noviello_funil.triagem_financeira import fatiar_mensagem

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("boletim: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("boletim: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    hoje = datetime.datetime.now(_BRT).date()
    if not settings.boletim_forcar and not na_janela_de_envio(hoje):
        logger.info("boletim: hoje (%s) não é o último dia útil — pulando", hoje)
        return 0

    comp = competencia(hoje)
    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        if conn.execute(
            "SELECT 1 FROM boletim_competencia WHERE competencia = ?", (comp,),
        ).fetchone():
            logger.info("boletim: competência %s já enviada — pulando", comp)
            return 0

        dados = _carregar_clientes(conn)
        candidatos = [
            (pn, info) for pn, info in dados["procs"].items()
            if not info["is_secret"] and info["telefones"]
        ]
        sem_tel = sum(
            1 for _pn, info in dados["procs"].items()
            if not info["is_secret"] and not info["telefones"]
        )
        logger.info(
            "boletim: %d processos candidatos (não sigilosos com telefone); "
            "%d sem telefone (de fora)", len(candidatos), sem_tel,
        )

        # DataJud em paralelo (infra da triagem) → movimentos por processo,
        # contabilizando cobertura (não confundir falha de consulta com
        # "sem movimentação").
        dj = httpx.Client(timeout=20.0)
        limiter = _RateLimiter(RATE_MIN_INTERVALO)
        movs_por_proc: dict = {}
        cobertura: Counter = Counter()
        plock = threading.Lock()

        def _consultar(par) -> None:
            pn, _info = par
            limiter.aguardar()
            movs, st = consultar_movimentos_datajud(dj, pn, settings.datajud_api_key)
            with plock:
                movs_por_proc[pn] = movs
                cobertura[st] += 1

        try:
            with ThreadPoolExecutor(max_workers=CONCORRENCIA) as ex:
                list(ex.map(_consultar, candidatos))
        finally:
            dj.close()

        falhas = sum(v for k, v in cobertura.items() if k != "ok")
        if candidatos and falhas > len(candidatos) * 0.5:
            logger.error(
                "boletim: %d/%d consultas DataJud falharam — não marca a "
                "competência, re-tenta na janela. cobertura=%s",
                falhas, len(candidatos), dict(cobertura),
            )
            return 0
        if falhas:
            logger.warning("boletim: cobertura parcial DataJud: %s", dict(cobertura))

        itens = []
        for pn, info in candidatos:
            movs_mes = movimentos_do_mes(
                movs_por_proc.get(pn, []), hoje.year, hoje.month
            )
            movel = melhor_movel(info["telefones"])
            tel = movel or max(info["telefones"], key=len)
            if esta_suprimido(conn, telefone=tel):
                continue
            ambiguo = any(
                len(dados["tel_pids"].get(t, set())) > 1 for t in info["telefones"]
            )
            multi = len(info["person_ids"]) > 1
            plano = classificar_boletim(
                movs_mes, telefone_ambiguo=ambiguo, multi_cliente=multi,
                telefone_movel=bool(movel),
            )
            if plano["modo"] == "skip":
                continue
            ultima = max(
                (str(m.get("data") or "")[:10] for m in movs_mes), default=""
            )
            # nome p/ o lote do Mario (NÃO vai na msg ao cliente): o 1º
            # cadastrado do processo. Em 'auto' só há 1 pessoa (multi→rascunho).
            nome = next(iter(info["nomes"].values()), None) or "Cliente"
            if plano["modo"] == "auto":
                link = wa_me_link(tel, montar_mensagem_cliente(pn, ultima))
            else:
                link = wa_me_link(tel)
            itens.append({
                "nome": nome, "processo": pn, "telefone": tel,
                "modo": plano["modo"], "motivo": plano["motivo"],
                "data": ultima, "link": link,
            })

        logger.info(
            "boletim %s: %d clientes (✅%d ⚠️%d)", comp, len(itens),
            sum(1 for i in itens if i["modo"] == "auto"),
            sum(1 for i in itens if i["modo"] == "rascunho"),
        )
        texto = montar_lote(itens, comp, sem_telefone=sem_tel)
        if texto is None:
            conn.execute(
                "INSERT OR IGNORE INTO boletim_competencia (competencia, total) "
                "VALUES (?, 0)", (comp,),
            )
            return 0

        async def _enviar() -> tuple[int, int]:
            jurichat = JurichatClient(
                api_key=settings.jurichat_api_key,
                base_url=settings.jurichat_base_url,
                bot_user_id=settings.jurichat_bot_user_id,
            )
            destinos = [
                c.strip() for c in settings.mario_conversation_id.split(",") if c.strip()
            ]
            blocos = fatiar_mensagem(texto)
            ok = 0
            try:
                for conv in destinos:
                    bom = True
                    for b in blocos:
                        try:
                            await jurichat.send_message(conv, b)
                        except (
                            OutboundError, httpx.HTTPStatusError, httpx.RequestError,
                        ) as exc:
                            logger.error(
                                "boletim: envio ao Mario falhou (%s): %s", conv, exc,
                            )
                            bom = False
                            break
                    if bom:
                        ok += 1
            finally:
                await jurichat.aclose()
            return ok, len(destinos)

        ok, total_dest = asyncio.run(_enviar())
        if ok:
            if ok < total_dest:
                logger.warning(
                    "boletim: só %d/%d destinos receberam (marca mesmo assim)",
                    ok, total_dest,
                )
            conn.execute(
                "INSERT OR IGNORE INTO boletim_competencia (competencia, total) "
                "VALUES (?, ?)", (comp, len(itens)),
            )
        else:
            logger.error("boletim: nenhum destino recebeu — não marca competência")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
