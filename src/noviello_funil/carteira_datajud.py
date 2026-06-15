"""Cruzamento Carteira × DataJud — pega falhas SILENCIOSAS de monitoramento.

O ``saude_carteira`` lê só o lado do Juridiq: alerta os ``monitoringStatus
== ERRO`` e os parados há +1 ano. Mas o Juridiq também falha em SILÊNCIO —
mantém o processo como ``CADASTRADO`` (status "OK") e simplesmente para de
trazer andamentos. A tela do Juridiq não avisa; só comparando com o
DataJud (CNJ) dá pra ver que o tribunal andou e o Juridiq ficou pra trás.

Exemplo real (auditoria 2026-06-14): 1007599-44.2019.8.26.0248 parado no
Juridiq em 2020-12 (CADASTRADO, "saudável") enquanto o tribunal arquivou
definitivamente em 2026-03. O saude_carteira não pega; este job pega.

Regra: alerta quando o DataJud está > ``limiar_dias`` à frente do Juridiq
E o status NÃO é ERRO (ERRO já vai no saude_carteira — não duplicamos).
Idempotente: 🆕 só nos que entraram desde a última execução.

Execução: console script ``noviello-carteira-datajud`` via systemd timer
semanal (segunda 06h BRT). É mais lento que o saude_carteira (consulta o
DataJud processo a processo, com throttle), por isso roda separado e cedo.

API DataJud: chave PÚBLICA oficial do CNJ (publicada em
https://datajud-wiki.cnj.jus.br/api-publica/acesso) — não é segredo.
"""

import datetime
import logging
import re
import time

import httpx

logger = logging.getLogger(__name__)

THROTTLE_S = 0.6     # rate limit documentado do DataJud: 120 req/min
MAX_LISTA = 25       # cap de itens detalhados na mensagem

# Mapa J.TR do número CNJ (NNNNNNN-DD.AAAA.J.TR.OOOO) → alias do endpoint
# DataJud. Mesmo mapa do scripts/auditar_processos_datajud.py.
_TRIBUNAIS = {
    ("8", "26"): "tjsp", ("8", "19"): "tjrj", ("8", "13"): "tjmg",
    ("8", "21"): "tjrs", ("8", "16"): "tjpr", ("8", "24"): "tjsc",
    ("8", "07"): "tjdft", ("8", "05"): "tjba", ("8", "17"): "tjpe",
    ("8", "06"): "tjce", ("8", "09"): "tjgo", ("8", "08"): "tjes",
    ("8", "15"): "tjpb", ("8", "20"): "tjrn", ("8", "02"): "tjal",
    ("8", "25"): "tjse", ("8", "27"): "tjto", ("8", "10"): "tjma",
    ("8", "22"): "tjro", ("8", "01"): "tjac", ("8", "04"): "tjam",
    ("8", "14"): "tjpa", ("8", "03"): "tjap", ("8", "23"): "tjrr",
    ("8", "11"): "tjmt", ("8", "12"): "tjms", ("8", "18"): "tjpi",
    ("4", "01"): "trf1", ("4", "02"): "trf2", ("4", "03"): "trf3",
    ("4", "04"): "trf4", ("4", "05"): "trf5", ("4", "06"): "trf6",
    ("5", "02"): "trt2", ("5", "15"): "trt15",
    ("3", "00"): "stj",
}


def _so_digitos(s: str) -> str:
    return re.sub(r"\D", "", s)


def _alias_datajud(process_number: str) -> str | None:
    m = re.match(
        r"^\d{7}-?\d{2}\.?(\d{4})\.?(\d)\.?(\d{2})\.?\d{4}$",
        (process_number or "").strip(),
    )
    if not m:
        return None
    return _TRIBUNAIS.get((m.group(2), m.group(3)))


def _data(iso: object) -> datetime.date | None:
    """ISO (ou datetime com Z) → date. None se vazio/inválido."""
    if not iso:
        return None
    try:
        return datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return None


def _responsavel(p: dict) -> str:
    nomes = [r.get("name", "") for r in (p.get("responsibles") or []) if r.get("name")]
    return ", ".join(nomes) or "—"


def consultar_datajud(
    client: httpx.Client, process_number: str, api_key: str,
) -> tuple[str | None, str]:
    """Última movimentação no DataJud → (data_iso | None, status_consulta)."""
    alias = _alias_datajud(process_number)
    if not alias:
        return None, "tribunal_nao_mapeado"
    try:
        r = client.post(
            f"https://api-publica.datajud.cnj.jus.br/api_publica_{alias}/_search",
            headers={
                "Authorization": f"APIKey {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "query": {"match": {"numeroProcesso": _so_digitos(process_number)}},
                "size": 5,
            },
        )
        if r.status_code >= 400:
            return None, f"http_{r.status_code}"
        hits = r.json().get("hits", {}).get("hits", [])
        datas = [
            mov["dataHora"]
            for h in hits
            for mov in (h.get("_source") or {}).get("movimentos", []) or []
            if mov.get("dataHora")
        ]
        if not datas:
            return None, "sem_movimentos"
        return max(datas), "ok"
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        # ValueError = 200 com corpo não-JSON; KeyError/TypeError = payload
        # inesperado. Não pode derrubar a varredura dos outros processos.
        return None, f"erro_{type(exc).__name__}"


def eh_silenciosa(
    jq_iso: object, dj_iso: object, monitoring_status: object, limiar_dias: int,
) -> bool:
    """True quando o tribunal está à frente do Juridiq e ninguém avisou.

    Exclui ERRO de propósito: esse já é alertado pelo saude_carteira.
    """
    if monitoring_status == "ERRO":
        return False
    dj = _data(dj_iso)
    if dj is None:
        return False
    jq = _data(jq_iso)
    if jq is None:
        return True
    return (dj - jq).days > limiar_dias


def classificar(processos: list[dict], limiar_dias: int) -> list[dict]:
    """Filtra as falhas silenciosas e ordena por movimento mais recente.

    Cada processo precisa ter ``dj_date`` (a última mov. do DataJud já
    consultada). Mais recente no tribunal primeiro = prazo correndo agora.
    """
    flags = []
    for p in processos:
        jq_iso = p.get("lastMovementDate")
        dj_iso = p.get("dj_date")
        if not eh_silenciosa(jq_iso, dj_iso, p.get("monitoringStatus"), limiar_dias):
            continue
        jq, dj = _data(jq_iso), _data(dj_iso)
        atraso = (dj - jq).days if jq else "sem data no Juridiq"
        flags.append({
            "processo": p.get("processNumber") or "(sem número)",
            "responsavel": _responsavel(p),
            "jq_mov": str(jq_iso or "")[:10],
            "trib_mov": str(dj_iso or "")[:10],
            "atraso_dias": atraso,
            "status": p.get("monitoringStatus"),
        })
    flags.sort(key=lambda f: f["trib_mov"], reverse=True)
    return flags


def diff_novos(conn, atuais: list[str]) -> set[str]:
    """Quais process_numbers entraram em falha silenciosa desde a última vez.

    Mesma mecânica do saude_carteira: insere novos, remove resolvidos
    (se voltarem a falhar, contam como novos de novo).
    """
    atuais_set = set(atuais)
    vistos = {
        r[0] for r in conn.execute(
            "SELECT process_number FROM carteira_datajud_visto"
        ).fetchall()
    }
    novos = atuais_set - vistos
    resolvidos = vistos - atuais_set
    for num in novos:
        conn.execute(
            "INSERT OR IGNORE INTO carteira_datajud_visto (process_number) VALUES (?)",
            (num,),
        )
    for num in resolvidos:
        conn.execute(
            "DELETE FROM carteira_datajud_visto WHERE process_number = ?", (num,),
        )
    return novos


def montar_mensagem(falhas: list[dict], novos: set[str]) -> str | None:
    """Mensagem WhatsApp-ready, ou None se não há falha silenciosa."""
    if not falhas:
        return None
    blocos = [
        "🔍 *Carteira × DataJud — falhas silenciosas de monitoramento*",
        "\nO Juridiq mostra estes como OK, mas o tribunal andou e o "
        "andamento NÃO entrou (re-sincronizar no painel):",
    ]
    for f in falhas[:MAX_LISTA]:
        marca = " 🆕" if f["processo"] in novos else ""
        if isinstance(f["atraso_dias"], int):
            atraso = f"{f['atraso_dias']} dias atrás"
        else:
            atraso = f["atraso_dias"]
        blocos.append(
            f"• {f['processo']} — {f['responsavel']} "
            f"(Juridiq: {f['jq_mov'] or '—'} / tribunal: {f['trib_mov']}, {atraso}){marca}"
        )
    if len(falhas) > MAX_LISTA:
        blocos.append(f"… e mais {len(falhas) - MAX_LISTA}.")
    blocos.append(
        "\nTriagem automática (fonte: DataJud/CNJ) — confira no painel antes de agir."
    )
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


def main() -> int:
    """Entry point do console script ``noviello-carteira-datajud``.

    Carteira sem falha silenciosa → exit 0 silencioso (nenhum envio).
    """
    import asyncio

    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("carteira_datajud: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("carteira_datajud: MARIO_CONVERSATION_ID não configurado — pulando")
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

    # Anexa a última mov. do DataJud a cada processo (consulta a consulta).
    dj = httpx.Client(timeout=30.0)
    try:
        for i, p in enumerate(processos, 1):
            num = p.get("processNumber") or ""
            if num:
                dj_data, _ = consultar_datajud(dj, num, settings.datajud_api_key)
                p["dj_date"] = dj_data
            if i % 50 == 0:
                logger.info("carteira_datajud: %d/%d consultados", i, len(processos))
            time.sleep(THROTTLE_S)
    finally:
        dj.close()

    flags = classificar(processos, settings.carteira_datajud_limiar_dias)
    logger.info(
        "carteira_datajud: %d falhas silenciosas (de %d processos)",
        len(flags), len(processos),
    )

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        novos = diff_novos(conn, [f["processo"] for f in flags])
    finally:
        conn.close()

    texto = montar_mensagem(flags, novos)
    if texto is None:
        return 0
    logger.info("carteira_datajud:\n%s", texto)

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
