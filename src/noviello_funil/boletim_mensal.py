"""Boletim mensal de andamento ao cliente (roadmap 3.1 — variante mensal).

No ÚLTIMO DIA ÚTIL do mês, monta um resumo do andamento processual dos
clientes que tiveram movimentação no mês e prepara o envio. Decisões do
Mario (15/jun):
  - HÍBRIDO: os casos claramente seguros (movimentação procedimental, não
    sigiloso, telefone autenticado) saem como "✅ pronto pra enviar"; os
    sensíveis (penhora/leilão/sentença/dinheiro) ou de telefone ambíguo vão
    como "⚠️ revisar antes".
  - Processo SEM movimentação no mês → não entra (nada de "nada aconteceu").

v1 (este): tudo vai pro Mario num lote de revisão (notify_mario, que sempre
funciona), com a classificação ✅/⚠️ e um link wa.me com a mensagem pronta
pra ele tocar e enviar. É a 1ª rodada revisável + o mecanismo wa.me que já
funciona (igual aniversários). Quando o endpoint de envio PROATIVO do
Jurichat for confirmado, é só ligar o auto-envio dos ✅ (flag).

Travas OAB (reuso do 2.4): só telefone autenticado por person_id
(``cliente_processo``); pula segredo de justiça; respeita opt-out (1.10);
conteúdo sóbrio, sem opinião/prognóstico/prazo; "nossa equipe", nunca "Dr.
Mario". Idempotente por competência (YYYY-MM).
"""

import calendar
import datetime
import logging
import re
import threading
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

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

MAX_LISTA = 40  # cap de itens por bloco na mensagem ao Mario

# Movimentações que NÃO são pra auto-enviar cru ao cliente — pedem a mão do
# advogado (desfecho/decisão). Constrição e dinheiro reusam a triagem.
_RX_DESFECHO = [
    re.compile(p)
    for p in [
        r"\bsenten[çc]a\b",
        r"\bac[óo]rd[ãa]o\b",
        r"\btr[âa]nsito\s+em\s+julgado\b",
        r"\bimproced[êe]nte\b",
        r"\bproced[êe]nte\b",
        r"\bextin[çc]\w+",
        r"\barquivamento\s+definitivo\b",
        r"\bbaixa\s+definitiva\b",
        r"\bhomologa[çc]\w+\s+de\s+acordo\b",
        r"\bdesist[êe]ncia\b",
    ]
]


def ultimo_dia_util_do_mes(d: datetime.date) -> datetime.date:
    """Último dia ÚTIL (seg-sex) do mês de ``d``. Feriados não considerados
    (refinamento futuro); na prática o job roda na janela até o fim do mês."""
    ultimo = calendar.monthrange(d.year, d.month)[1]
    dia = datetime.date(d.year, d.month, ultimo)
    while dia.weekday() >= 5:   # 5=sáb, 6=dom
        dia -= datetime.timedelta(days=1)
    return dia


def na_janela_de_envio(hoje: datetime.date) -> bool:
    """True do último dia útil até o fim do mês (dá margem de retry se o
    envio falhar no dia exato)."""
    return hoje >= ultimo_dia_util_do_mes(hoje)


def competencia(d: datetime.date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def _data(iso: object) -> datetime.date | None:
    if not iso:
        return None
    try:
        return datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return None


def movimentos_do_mes(
    movimentos: list[dict], ano: int, mes: int,
) -> list[dict]:
    """Movimentos cuja data cai no mês/ano dado."""
    out = []
    for m in movimentos or []:
        d = _data(m.get("data"))
        if d and d.year == ano and d.month == mes:
            out.append(m)
    return out


def eh_sensivel(movimentos_mes: list[dict]) -> str | None:
    """Se algum movimento do mês é sensível (não auto-enviável cru), devolve
    o motivo ('constrição'|'dinheiro'|'desfecho'); senão None."""
    for m in movimentos_mes:
        txt = str(m.get("nome") or "").lower()
        if any(rx.search(txt) for rx in _RX_CONSTRICAO):
            return "constrição"
        if any(rx.search(txt) for rx in _RX_LEVANTAR):
            return "dinheiro a levantar"
        if any(rx.search(txt) for rx in _RX_DESFECHO):
            return "desfecho/decisão"
    return None


def classificar_boletim(
    movimentos_mes: list[dict], telefone_ambiguo: bool,
) -> dict:
    """Decide o modo do boletim de UM processo.

    skip (sem movimento) | rascunho (sensível ou ambíguo) | auto.
    """
    if not movimentos_mes:
        return {"modo": "skip", "motivo": "sem movimentação no mês"}
    if telefone_ambiguo:
        return {"modo": "rascunho", "motivo": "telefone bate com +1 cadastro"}
    motivo = eh_sensivel(movimentos_mes)
    if motivo:
        return {"modo": "rascunho", "motivo": motivo}
    return {"modo": "auto", "motivo": ""}


def _fmt_data(iso: object) -> str:
    s = str(iso or "")[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    return f"{m.group(3)}/{m.group(2)}/{m.group(1)}" if m else s


def montar_mensagem_cliente(process_number: str, data_ultima_mov: object) -> str:
    """Mensagem sóbria ao cliente (a que vai no wa.me dos '✅ pronto').

    Fato puro — sem opinião, prognóstico ou prazo. "nossa equipe", e uma
    saída de opt-out (o '/SAIR' já é capturado pelo handler de opt-out)."""
    quando = f" em {_fmt_data(data_ultima_mov)}" if data_ultima_mov else " este mês"
    return (
        "Olá! 👋 Passando pra te dar um retorno do seu processo "
        f"nº {process_number}: houve movimentação{quando} e nossa equipe "
        "segue acompanhando de perto. Qualquer dúvida, é só chamar por aqui. 🙏\n"
        "(Se preferir não receber este resumo mensal, responda SAIR.)"
    )


def _e164(telefone_chave: str) -> str:
    """telefone_chave (DDD+número) → '55DDDNUMERO' p/ wa.me."""
    return "55" + re.sub(r"\D", "", telefone_chave)


def wa_me_link(telefone_chave: str, mensagem: str = "") -> str:
    base = f"https://wa.me/{_e164(telefone_chave)}"
    if mensagem:
        return base + "?text=" + urllib.parse.quote(mensagem)
    return base


def montar_lote(itens: list[dict], comp: str) -> str | None:
    """Lote de revisão ao Mario. ``itens`` = [{nome, processo, telefone,
    modo, motivo, data, link}]. None se não há nada."""
    if not itens:
        return None
    auto = [i for i in itens if i["modo"] == "auto"]
    rasc = [i for i in itens if i["modo"] == "rascunho"]
    blocos = [
        f"🗓️ *Boletim mensal — {comp}* (último dia útil)",
        "\nClientes com movimentação no mês. Toque o link pra enviar pelo WhatsApp:",
    ]
    if auto:
        blocos.append(f"\n✅ *Prontos pra enviar* ({len(auto)})")
        for i in auto[:MAX_LISTA]:
            blocos.append(
                f"• {i['nome']} — proc {i['processo']} "
                f"(mov {_fmt_data(i['data'])}): {i['link']}"
            )
        if len(auto) > MAX_LISTA:
            blocos.append(f"… e mais {len(auto) - MAX_LISTA}.")
    if rasc:
        blocos.append(f"\n⚠️ *Revisar antes — escreva você* ({len(rasc)})")
        for i in rasc[:MAX_LISTA]:
            blocos.append(
                f"• {i['nome']} — proc {i['processo']} ({i['motivo']}): {i['link']}"
            )
        if len(rasc) > MAX_LISTA:
            blocos.append(f"… e mais {len(rasc) - MAX_LISTA}.")
    blocos.append(
        f"\nTotal: {len(itens)} clientes. Pulei sem-movimentação, sigilosos e "
        "quem deu opt-out. Os ✅ já vêm com a mensagem pronta no link."
    )
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
            "nome": nome, "telefones": set(),
        })
        e["person_ids"].add(pid)
        if tel:
            e["telefones"].add(tel)
            tel_pids[tel].add(pid)
    return {"procs": procs, "tel_pids": tel_pids}


def main() -> int:
    """Entry point do console script ``noviello-boletim``."""
    import asyncio
    from zoneinfo import ZoneInfo

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

    hoje = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
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
        # Processos não sigilosos, com telefone — candidatos à varredura.
        candidatos = [
            (pn, info) for pn, info in dados["procs"].items()
            if not info["is_secret"] and info["telefones"]
        ]
        logger.info("boletim: %d processos candidatos (não sigilosos)", len(candidatos))

        # DataJud em paralelo (mesma infra da triagem) → movimentos por processo.
        dj = httpx.Client(timeout=20.0)
        limiter = _RateLimiter(RATE_MIN_INTERVALO)
        movs_por_proc: dict = {}
        plock = threading.Lock()

        def _consultar(par) -> None:
            pn, _info = par
            limiter.aguardar()
            movs, _ = consultar_movimentos_datajud(dj, pn, settings.datajud_api_key)
            with plock:
                movs_por_proc[pn] = movs

        try:
            with ThreadPoolExecutor(max_workers=CONCORRENCIA) as ex:
                list(ex.map(_consultar, candidatos))
        finally:
            dj.close()

        itens = []
        for pn, info in candidatos:
            movs_mes = movimentos_do_mes(movs_por_proc.get(pn, []), hoje.year, hoje.month)
            tel = max(info["telefones"], key=len)   # forma com 9º dígito
            if esta_suprimido(conn, telefone=tel):
                continue
            ambiguo = any(len(dados["tel_pids"].get(t, set())) > 1 for t in info["telefones"])
            plano = classificar_boletim(movs_mes, ambiguo)
            if plano["modo"] == "skip":
                continue
            ultima = max((str(m.get("data") or "")[:10] for m in movs_mes), default="")
            nome = info["nome"] or "Cliente"
            if plano["modo"] == "auto":
                link = wa_me_link(tel, montar_mensagem_cliente(pn, ultima))
            else:
                link = wa_me_link(tel)   # rascunho: Mario escreve
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
        texto = montar_lote(itens, comp)
        if texto is None:
            # Nada a enviar, mas marca a competência (não re-varre o mês todo).
            conn.execute(
                "INSERT OR IGNORE INTO boletim_competencia (competencia, total) "
                "VALUES (?, 0)", (comp,),
            )
            return 0

        async def _enviar() -> int:
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
                            logger.error("boletim: envio ao Mario falhou (%s): %s", conv, exc)
                            bom = False
                            break
                    if bom:
                        ok += 1
            finally:
                await jurichat.aclose()
            return ok

        if asyncio.run(_enviar()):
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
