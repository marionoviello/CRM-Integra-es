"""Saúde da carteira de processos — alerta semanal ao Mario.

Cobre dois riscos que o Juridiq mostra mas não empurra:

1. MONITORAMENTO FALHANDO (roadmap 1.2): o Juridiq marca cada processo
   com ``monitoringStatus``. Os que estão em ``ERRO`` têm o
   monitoramento quebrado — os andamentos NÃO estão entrando, em
   silêncio. Em jun/2026 eram 48 na carteira. Lemos direto (sem
   precisar cruzar com o DataJud) e alertamos.

2. PROCESSOS PARADOS (roadmap 1.3): processos com monitoramento OK mas
   sem movimentação há mais de 1 ano — sinal clássico de risco de
   prescrição intercorrente. O campo ``lastMovementDate`` já vem na
   listagem.

O alerta é TRIAGEM, não parecer: diz "religar" / "avaliar", nunca
"prescreveu". Idempotência: destaca 🆕 só os erros novos desde a última
execução, pra não repetir a mesma lista toda semana.

Execução: console script ``noviello-saude-carteira`` via systemd timer
semanal (segunda 09h BRT, logo após o relatório de funil).
"""

import asyncio
import datetime
import logging
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

# Acima disso, um processo que monitora OK mas não anda entra no radar
# de prescrição. 1 ano é o piso conservador (Mario refina depois).
LIMITE_PARADO_DIAS = 365
MAX_ERRO = 20       # cap de itens detalhados (o total vai sempre no topo)
MAX_PARADOS = 12


def _dias_desde(iso: object, hoje: datetime.date) -> int | None:
    try:
        d = datetime.date.fromisoformat(str(iso)[:10])
    except (ValueError, TypeError):
        return None
    return (hoje - d).days


def _responsavel(p: dict) -> str:
    nomes = [r.get("name", "") for r in (p.get("responsibles") or []) if r.get("name")]
    return ", ".join(nomes) or "—"


def classificar_carteira(processos: list[dict], hoje: datetime.date) -> dict:
    """Separa a carteira em {erro, parados}.

    - erro: monitoringStatus == ERRO (monitoramento quebrado).
    - parados: monitoramento OK (CADASTRADO), não segredo, sem
      movimentação há > LIMITE_PARADO_DIAS. (Processos em ERRO ficam
      fora de "parados" — o lastMovementDate deles não é confiável
      justamente porque o monitoramento falhou.)
    """
    erro, parados = [], []
    for p in processos:
        ms = p.get("monitoringStatus")
        num = p.get("processNumber") or "(sem número)"
        if ms == "ERRO":
            erro.append({
                "processo": num,
                "responsavel": _responsavel(p),
                "ultima_mov": str(p.get("lastMovementDate") or "")[:10],
            })
        elif ms == "CADASTRADO" and not p.get("isSecret"):
            d = _dias_desde(p.get("lastMovementDate"), hoje)
            if d is not None and d > LIMITE_PARADO_DIAS:
                parados.append({
                    "processo": num,
                    "responsavel": _responsavel(p),
                    "dias": d,
                })
    parados.sort(key=lambda x: -x["dias"])
    return {"erro": erro, "parados": parados}


def diff_novos_erros(conn, erros_atuais: list[str]) -> set[str]:
    """Quais process_numbers entraram em ERRO desde a última execução.

    Atualiza a tabela: insere os novos, remove os que saíram de ERRO
    (assim, se voltarem a dar erro no futuro, contam como novos de novo).
    """
    atuais = set(erros_atuais)
    vistos = {
        r[0] for r in conn.execute(
            "SELECT process_number FROM carteira_erro_visto"
        ).fetchall()
    }
    novos = atuais - vistos
    resolvidos = vistos - atuais
    for num in novos:
        conn.execute(
            "INSERT OR IGNORE INTO carteira_erro_visto (process_number) VALUES (?)",
            (num,),
        )
    for num in resolvidos:
        conn.execute(
            "DELETE FROM carteira_erro_visto WHERE process_number = ?", (num,),
        )
    return novos


def montar_mensagem(diag: dict, novos_erros: set[str]) -> str | None:
    """Mensagem WhatsApp-ready, ou None se a carteira está saudável."""
    erro, parados = diag["erro"], diag["parados"]
    if not erro and not parados:
        return None

    blocos = ["🩺 *Saúde da carteira de processos*"]

    if erro:
        blocos.append(
            f"\n⚠️ *{len(erro)} com monitoramento FALHANDO* "
            "(andamentos não estão entrando — religar no Juridiq):"
        )
        for e in erro[:MAX_ERRO]:
            marca = " 🆕" if e["processo"] in novos_erros else ""
            ult = f" (última mov.: {e['ultima_mov']})" if e["ultima_mov"] else ""
            blocos.append(f"• {e['processo']} — {e['responsavel']}{ult}{marca}")
        if len(erro) > MAX_ERRO:
            blocos.append(f"… e mais {len(erro) - MAX_ERRO}.")

    if parados:
        blocos.append(
            f"\n🕰️ *{len(parados)} parados há +1 ano* "
            "(monitoram OK — avaliar impulso/prescrição):"
        )
        for p in parados[:MAX_PARADOS]:
            anos = p["dias"] / 365
            blocos.append(
                f"• {p['processo']} — {p['responsavel']} "
                f"(parado há {p['dias']} dias ≈ {anos:.1f} ano(s))"
            )
        if len(parados) > MAX_PARADOS:
            blocos.append(f"… e mais {len(parados) - MAX_PARADOS}.")

    blocos.append(
        "\nTriagem automática (fonte: Juridiq) — confira no painel antes de agir."
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
    """Entry point do console script ``noviello-saude-carteira``.

    Carteira saudável → exit 0 silencioso (nenhum envio).
    """
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations
    from noviello_funil.outbound import JurichatClient, notify_mario

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    if not settings.juridiq_api_key:
        logger.warning("saude_carteira: JURIDIQ_API_KEY não configurada — pulando")
        return 0
    if (
        not settings.mario_conversation_id
        or settings.mario_conversation_id == "placeholder-pendente"
    ):
        logger.warning("saude_carteira: MARIO_CONVERSATION_ID não configurado — pulando")
        return 0

    client = httpx.Client(
        base_url=settings.juridiq_base_url,
        headers={"x-juridiq-api-key": settings.juridiq_api_key},
        timeout=30.0,
    )
    try:
        processos = _listar_processos(client)
    finally:
        client.close()

    hoje = datetime.datetime.now(ZoneInfo("America/Sao_Paulo")).date()
    diag = classificar_carteira(processos, hoje)
    logger.info(
        "saude_carteira: %d em ERRO, %d parados (de %d processos)",
        len(diag["erro"]), len(diag["parados"]), len(processos),
    )

    conn = connect(settings.database_path)
    run_migrations(conn)
    try:
        novos = diff_novos_erros(conn, [e["processo"] for e in diag["erro"]])
    finally:
        conn.close()

    texto = montar_mensagem(diag, novos)
    if texto is None:
        return 0
    logger.info("saude_carteira:\n%s", texto)

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
