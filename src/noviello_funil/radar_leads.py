"""Radar de leads — relatório 9h/15h + alerta de documento sem resposta.

Pedido Mario 30/ago (caso Paulo: lead parado desde 19/ago sem ninguém ver):

1. Relatório às 9h e 15h BRT nos canais de alerta (Mario + Hilde): quem está
   esperando resposta NOSSA, há quanto tempo, e o panorama por estado.
2. Alerta 🚨 (varredura a cada ~15 min): lead mandou DOCUMENTO/arquivo e
   ninguém respondeu em 2h — uma vez por documento (idempotente por
   message_id na tabela ``radar_docs_alertados``).

Só leitura no Jurichat + notificações internas; nada vai ao lead. Fora do
radar: os próprios canais de alerta e leads AGUARDANDO_HUMANO por motivo de
exclusão deliberada (baseline importada, tag de exclusão, responsável
próprio, canal de alerta). opt_out/encerrado_a_pedido ficam FORA da lista
de espera do relatório (lead pediu silêncio), mas documento deles ainda
alerta — quem manda arquivo voltou a procurar o escritório.

"Documento" = mensagem INBOUND cujo ``type`` não é text nem audio (áudio é
conversa normal, transcrita e respondida pelo bot).
"""

import datetime
import logging
import zoneinfo
from typing import Any

from noviello_funil.outbound import JurichatClient, notify_mario, split_conversation_ids
from noviello_funil.state import Estado, deve_alertar_global

logger = logging.getLogger(__name__)

_TZ_BRT = zoneinfo.ZoneInfo("America/Sao_Paulo")

# AGUARDANDO_HUMANO por decisão de sistema — não são leads em atendimento.
_MOTIVOS_FORA_DO_RADAR = frozenset({
    "canal_alertas_mario",
    "baseline_first_sync",
    "filtro_tem_responsavel",
    "filtro_tag_exclusao",
    "excluido_followup_etiqueta",
})
# Lead pediu silêncio: sem cobrança no relatório (mas documento alerta).
_MOTIVOS_SEM_COBRANCA = frozenset({"opt_out", "encerrado_a_pedido"})
_TIPOS_CONVERSA = frozenset({"text", "audio"})
# Espera mínima pra aparecer no relatório — em_conversa saudável tem leads
# de minutos (anti-rajada/espera-humano); só interessa o que envelheceu.
_ESPERA_MINIMA = datetime.timedelta(minutes=30)
_MAX_LINHAS_RELATORIO = 15


def _parse_at(bruto: Any) -> datetime.datetime | None:
    """messageAt ISO → datetime aware (UTC assumido se vier naive)."""
    if not isinstance(bruto, str) or not bruto:
        return None
    try:
        dt = datetime.datetime.fromisoformat(bruto.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt


def analisar_mensagens(messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Do ``messages_raw`` (ordem cronológica) extrai o estado de espera.

    Retorna::

        {"espera_desde": dt|None,   # 1ª msg do lead sem resposta posterior
         "n_sem_resposta": int,
         "doc_desde": dt|None,      # 1º documento entre as sem-resposta
         "doc_id": str, "doc_tipo": str}
    """
    ultima_saida = -1
    for i, msg in enumerate(messages):
        if msg.get("direction") != "INBOUND":
            ultima_saida = i
    sem_resposta = [
        m for m in messages[ultima_saida + 1:] if m.get("direction") == "INBOUND"
    ]

    espera_desde = None
    doc_desde = None
    doc_id = ""
    doc_tipo = ""
    for m in sem_resposta:
        at = _parse_at(m.get("messageAt"))
        if at is None:
            continue
        if espera_desde is None or at < espera_desde:
            espera_desde = at
        tipo = (m.get("type") or "text").lower()
        if tipo not in _TIPOS_CONVERSA and (doc_desde is None or at < doc_desde):
            doc_desde = at
            doc_id = m.get("id") or ""
            doc_tipo = tipo
    return {
        "espera_desde": espera_desde,
        "n_sem_resposta": len(sem_resposta),
        "doc_desde": doc_desde,
        "doc_id": doc_id,
        "doc_tipo": doc_tipo,
    }


def _fmt_espera(delta: datetime.timedelta) -> str:
    total_min = max(0, int(delta.total_seconds() // 60))
    dias, resto = divmod(total_min, 1440)
    horas, minutos = divmod(resto, 60)
    if dias:
        return f"{dias}d {horas}h" if horas else f"{dias}d"
    if horas:
        return f"{horas}h{minutos:02d}" if minutos else f"{horas}h"
    return f"{minutos}min"


def _rotulo_estado(estado: str, motivo_ah: str) -> str:
    if estado == Estado.EM_CONVERSA:
        return "com a Julia"
    if estado in (Estado.FOLLOW_UP_1_ENVIADO, Estado.FOLLOW_UP_2_ENVIADO):
        return "em follow-up"
    if estado == Estado.AGUARDANDO_HUMANO:
        return "com humano"
    return estado


def _listar_leads_radar(conn: Any) -> list[Any]:
    """Leads ativos que o radar acompanha (com o último motivo de AH)."""
    rows = conn.execute(
        """
        SELECT l.*, (
            SELECT t.motivo FROM transicoes t
            WHERE t.lead_id = l.id AND t.estado_novo = ?
            ORDER BY t.id DESC LIMIT 1
        ) AS motivo_ah
        FROM leads l
        WHERE l.estado IN (?, ?, ?, ?)
        """,
        (
            Estado.AGUARDANDO_HUMANO,
            Estado.EM_CONVERSA,
            Estado.FOLLOW_UP_1_ENVIADO,
            Estado.FOLLOW_UP_2_ENVIADO,
            Estado.AGUARDANDO_HUMANO,
        ),
    ).fetchall()
    return [
        r for r in rows
        if not (
            r["estado"] == Estado.AGUARDANDO_HUMANO
            and (r["motivo_ah"] or "") in _MOTIVOS_FORA_DO_RADAR
        )
    ]


def _marcar_doc_alertado(conn: Any, message_id: str, lead_id: int) -> bool:
    """True se este documento AINDA não tinha sido alertado (e carimba)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO radar_docs_alertados (message_id, lead_id) "
        "VALUES (?, ?)",
        (message_id, lead_id),
    )
    conn.commit()
    return cur.rowcount == 1


def _montar_relatorio(
    entradas: list[tuple[Any, dict[str, Any]]],
    agora: datetime.datetime,
) -> str:
    esperando: list[tuple[datetime.timedelta, Any, dict[str, Any]]] = []
    docs_pendentes = 0
    rotulos: dict[str, int] = {}
    for lead, info in entradas:
        rotulo = _rotulo_estado(lead["estado"], lead["motivo_ah"] or "")
        rotulos[rotulo] = rotulos.get(rotulo, 0) + 1
        if info["doc_desde"] is not None:
            docs_pendentes += 1
        if (
            lead["estado"] == Estado.AGUARDANDO_HUMANO
            and (lead["motivo_ah"] or "") in _MOTIVOS_SEM_COBRANCA
        ):
            continue
        if info["espera_desde"] is None:
            continue
        idade = agora - info["espera_desde"].astimezone(_TZ_BRT)
        if idade < _ESPERA_MINIMA:
            continue
        esperando.append((idade, lead, info))
    esperando.sort(key=lambda e: e[0], reverse=True)

    linhas = [f"📊 *Radar de leads — {agora.strftime('%d/%m %Hh')}*", ""]
    if esperando:
        linhas.append(f"⏳ *Esperando resposta nossa ({len(esperando)}):*")
        for idade, lead, info in esperando[:_MAX_LINHAS_RELATORIO]:
            nome = lead["contato_nome"] or lead["contato_telefone"]
            doc = " 📎" if info["doc_desde"] is not None else ""
            rotulo = _rotulo_estado(lead["estado"], lead["motivo_ah"] or "")
            linhas.append(f"• {nome} — {_fmt_espera(idade)} ({rotulo}){doc}")
        if len(esperando) > _MAX_LINHAS_RELATORIO:
            linhas.append(f"… e mais {len(esperando) - _MAX_LINHAS_RELATORIO}.")
    else:
        linhas.append("✅ Ninguém esperando resposta há mais de 30 min.")
    if docs_pendentes:
        linhas.append(f"\n📎 Com documento sem resposta: {docs_pendentes}")
    panorama = " · ".join(f"{rot}: {n}" for rot, n in sorted(rotulos.items()))
    linhas.append(f"\nPanorama: {panorama or 'nenhum lead ativo'}")
    return "\n".join(linhas)


async def run_radar_leads(
    *,
    get_db: Any,
    jurichat: JurichatClient,
    mario_conversation_id: str,
    agora: datetime.datetime | None = None,
    varredura_min: int = 15,
    doc_alerta_horas: float = 2.0,
    relatorio_horas: tuple[int, ...] = (9, 15),
) -> None:
    """Uma varredura do radar (chamada a cada tick; o cooldown segura o ritmo).

    Best-effort: erro em um lead não derruba a varredura, e erro na varredura
    não pode derrubar o ciclo (caller nota o log, healthcheck segue pingando).
    """
    try:
        conn = get_db()
        if varredura_min and not deve_alertar_global(
            conn, "radar_leads_varredura", cooldown_min=varredura_min,
        ):
            return
        agora = (agora or datetime.datetime.now(_TZ_BRT)).astimezone(_TZ_BRT)
        canais = set(split_conversation_ids(mario_conversation_id))
        limite_doc = datetime.timedelta(hours=doc_alerta_horas)

        entradas: list[tuple[Any, dict[str, Any]]] = []
        for lead in _listar_leads_radar(conn):
            conv_id = lead["jurichat_conversation_id"]
            if conv_id in canais:
                continue
            try:
                conv = await jurichat.get_conversation(conv_id)
            except Exception as exc:
                logger.warning(
                    "radar: get_conversation falhou lead=%s: %s", lead["id"], exc,
                )
                continue
            info = analisar_mensagens(conv.get("messages_raw") or [])
            entradas.append((lead, info))

            if info["doc_desde"] is None or not info["doc_id"]:
                continue
            idade_doc = agora - info["doc_desde"].astimezone(_TZ_BRT)
            if idade_doc >= limite_doc and _marcar_doc_alertado(
                conn, info["doc_id"], lead["id"],
            ):
                nome = lead["contato_nome"] or lead["contato_telefone"]
                await notify_mario(
                    jurichat,
                    mario_conversation_id=mario_conversation_id,
                    mensagem=(
                        "🚨🚨 *URGENTE — documento sem resposta*\n\n"
                        f"Lead: {nome} ({lead['contato_telefone']})\n"
                        f"Enviou arquivo ({info['doc_tipo']}) há "
                        f"{_fmt_espera(idade_doc)} e ninguém respondeu.\n"
                        f"https://app.jurichat.com/messages?id={conv_id}"
                    ),
                )

        if agora.hour in set(relatorio_horas) and deve_alertar_global(
            conn, f"radar_relatorio_{agora.hour:02d}", cooldown_min=720,
        ):
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=_montar_relatorio(entradas, agora),
            )
    except Exception:
        logger.exception("radar de leads falhou — ciclo segue")
