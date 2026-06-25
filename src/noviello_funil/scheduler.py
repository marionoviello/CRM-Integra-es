"""Follow-up and poll scheduler — invoked every minute by systemd timer.

Two cycles share the same tick:

* ``run_poll_cycle`` — drives ACTIVE conversations. For every em_conversa
  lead whose ``proxima_acao_em`` is due, fetches the transcript, detects
  new messages by hash, then either reschedules (no new content), assumes
  Mario took over, hits the turn cap, or invokes Claude to decide the
  next action.

* ``run_followup_cycle`` — drives IDLE conversations. Sends the standard
  follow-up sequence (FU1 → FU2 → silent close) when a lead in
  em_conversa/follow_up_1/follow_up_2 has gone quiet long enough.

The two cycles are kept disjoint by ``list_leads_vencidos`` excluding
em_conversa leads with activity in the last 24h — see state.py.
"""

import asyncio
import datetime
import hashlib
import logging
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from noviello_funil.agendamento_match import casar_horario_escolhido
from noviello_funil.atendimento_processo import (
    MSG_NAO_CADASTRADO_CLIENTE,
    MSG_SIGILOSO_CLIENTE,
    alerta_ambiguo,
    alerta_nao_identificado,
    alerta_sigiloso,
    classificar_atendimento,
    consultar_processos_do_telefone,
    detectar_pergunta_status,
    montar_resposta_cliente,
    ultimo_movimento_datajud,
)
from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.briefing_reuniao import montar_briefing
from noviello_funil.calendar_client import (
    GoogleCalendarClient,
    GoogleCalendarError,
    Slot,
)
from noviello_funil.conflito import checar_conflito
from noviello_funil.juridiq_client import JuridiqClient, intake_lead_agendado
from noviello_funil.opt_out import (
    detectar_opt_out,
    esta_suprimido,
    registrar_opt_out,
)
from noviello_funil.outbound import (
    JurichatClient,
    format_notification,
    notify_mario,
    split_conversation_ids,
)
from noviello_funil.person_index import resolver_telefone
from noviello_funil.pos_assinatura import processar_pos_assinatura
from noviello_funil.redacao import contem_promessa_resultado
from noviello_funil.state import (
    CLEAR_PROXIMA_ACAO,
    Estado,
    bump_turnos,
    clear_horarios_oferecidos,
    clear_reuniao,
    create_lead_if_absent,
    event_ids_de_reunioes,
    evento_manual_ja_alertado,
    get_horarios_oferecidos,
    get_lead_by_conversation,
    lead_com_reuniao_no_horario,
    lead_por_email,
    list_contratos_pos_pendentes,
    list_leads_aguardando_humano,
    list_leads_com_reuniao_futura,
    list_leads_para_polling,
    list_leads_para_reativacao,
    list_leads_presos,
    list_leads_vencidos,
    marcar_ah_checado,
    marcar_erro_alertado,
    marcar_evento_manual_alertado,
    marcar_noshow_avisado,
    marcar_pos_travado,
    mark_cliente_checado,
    mark_lead_activity_now,
    mark_lembrete_enviado,
    mark_urgencia_alertada,
    register_error,
    registrar_tentativa_pos,
    reset_turnos,
    schedule_next_action_seconds,
    set_horarios_oferecidos,
    set_lead_email,
    set_reuniao,
    transicao,
    ultimo_motivo_transicao,
    update_transcript_hash,
)
from noviello_funil.urgencia import detectar_urgencia
from noviello_funil.zapsign_client import ZapSignClient

logger = logging.getLogger(__name__)

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _extrair_email(transcript: str) -> str | None:
    """Extrai primeiro email válido da transcrição. None se não houver."""
    m = _EMAIL_RE.search(transcript or "")
    return m.group(0) if m else None


async def ping_healthcheck(url: str) -> None:
    """Dead-man's switch: GET no healthchecks.io (ou similar).

    Fire-and-forget — falha de ping NUNCA derruba o ciclo (loga warning
    e segue). URL vazio = feature desligada, no-op silencioso.
    """
    if not url:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(url)
    except Exception as exc:
        logger.warning("healthcheck ping falhou: %s", exc)


OPT_IN_TAGS = frozenset({"Fazer Follow up", "Proposta enviada"})

FOLLOWUP_2_TEXT = (
    "{nome}, percebi que talvez não seja o momento certo. "
    "Posso encerrar nosso atendimento por aqui? "
    "Se preferir continuar depois, é só me chamar novamente."
)

# Default polling cadence. Overridable via run_poll_cycle parameter.
DEFAULT_POLL_INTERVAL_SECONDS = 60


@dataclass
class CalendarConfig:
    """Tudo que o scheduler precisa pra agendar via Google Calendar.

    Quando ``client is None`` (config Google não definida no .env), as
    ações ``oferecer_horarios`` e ``confirmar_horario`` viram handoff
    automático ("vou te conectar com o advogado pra marcar").
    """

    client: GoogleCalendarClient | None
    business_hours_start: int
    business_hours_end: int
    slot_min: int
    buffer_min: int
    lookahead_days: int
    num_slots: int
    # Janela de manhã opcional (0/0 = desligada). Quando ligada, o gerador
    # oferece manhã + tarde. Default 0 mantém os fallbacks (sem Google) intactos.
    morning_start: int = 0
    morning_end: int = 0
    # Timezone dos horários da agenda (e dos ISO vindos do Claude sem
    # offset). Auditoria 2026-06-10: naive astimezone() assumia UTC do
    # VPS — evento criado 3h errado.
    timezone: str = "America/Sao_Paulo"


def is_eligible_for_followup(tags: list[str]) -> bool:
    """Strictly opt-in OR no-tag rule per spec §7.2.b."""
    if not tags:
        return True
    tag_set = set(tags)
    return bool(tag_set & OPT_IN_TAGS)


# --- Poll cycle helpers --------------------------------------------------
#
# Transcript format assumed (confirmed against Jurichat sample 2026-06-07
# but worth re-validating in prod): one message per line, prefixed with
# either ``Lead:`` or ``Atendente:``. Lines that don't match either prefix
# are treated as continuation and ignored by the counters.


def _compute_hash(transcript: str) -> str:
    """sha256 hex of the transcript bytes."""
    return hashlib.sha256(transcript.encode("utf-8")).hexdigest()


def _last_line_from_atendente(transcript: str) -> bool:
    """True iff the LAST non-empty line begins with ``Atendente:``.

    Used to detect "Mario took over" — when the human operator sent the
    most recent message in the conversation, Claude should stop and hand
    off to AGUARDANDO_HUMANO.
    """
    for line in reversed(transcript.splitlines()):
        stripped = line.strip()
        if not stripped:
            continue
        return stripped.startswith("Atendente:")
    return False


def _last_lead_message(transcript: str) -> str:
    """Last ``Lead:`` message body (prefix stripped), or empty string."""
    for line in reversed(transcript.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("Lead:"):
            return stripped[len("Lead:") :].strip()
    return ""


def _extrair_email_do_lead(transcript: str) -> str:
    """Email da linha ``Lead:`` MAIS RECENTE que contenha ``@``, ou "".

    Diferente de ``_extrair_email`` (primeiro email do transcript inteiro):
    varre só as linhas do LEAD, de baixo pra cima. Evita mandar o convite
    Meet pro email do atendente/assinatura/terceiro citado no transcript
    (auditoria 16/jun, falso-positivo crítico do Signal 1.8). "" quando
    nenhuma linha do lead tem email → o guardrail de pedir-email atua.
    """
    for line in reversed(transcript.splitlines()):
        stripped = line.lstrip()
        if stripped.startswith("Lead:"):
            m = _EMAIL_RE.search(stripped[len("Lead:") :])
            if m:
                return m.group(0)
    return ""


# --- Calendar handlers --------------------------------------------------

def _parse_horario_confirmado(
    iso: object, timezone: str,
) -> tuple[datetime.datetime | None, str | None]:
    """Parse SEGURO do horario_escolhido_iso vindo do LLM.

    Auditoria 2026-06-10 — três bugs cobertos aqui:
      - HIGH: ISO sem offset ('2026-06-12T15:00:00') virava naive e o
        astimezone() downstream assumia UTC do VPS → evento criado 3h
        errado (lead lia '15h' no WhatsApp, convite chegava 12h BRT).
        Naive agora é interpretado no timezone da agenda.
      - HIGH: horário no PASSADO era aceito (Claude extraindo data velha
        de conversa retomada) → evento no passado + lembretes mortos.
      - MEDIUM: valor não-string (int/dict do LLM) estourava TypeError
        não tratado e envenenava o tick.

    Retorna ``(datetime aware, None)`` ou ``(None, codigo_erro)``.
    """
    if not isinstance(iso, str):
        return None, "claude_horario_iso_invalido"
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        return None, "claude_horario_iso_invalido"
    if dt.tzinfo is None:
        from zoneinfo import ZoneInfo
        dt = dt.replace(tzinfo=ZoneInfo(timezone))
    agora = datetime.datetime.now(datetime.UTC)
    if dt <= agora + datetime.timedelta(minutes=5):
        return None, "horario_no_passado"
    return dt, None


def _format_slots_human(slots: list[Slot]) -> str:
    """Formata slots como bullet list pro WhatsApp."""
    return "\n".join(f"• {s.format_human()}" for s in slots)


# G4 (2026-06-16): mensagem que o bot manda ao lead ANTES de um handoff
# humano (claude_handoff / max_turnos) — antes esses caminhos eram MUDOS e o
# lead ficava sem resposta até alguém assumir. GENÉRICA de propósito: o
# max_turnos dispara em qualquer conversa longa (nem sempre agendamento) e o
# claude_handoff já manda a mensagem do Claude antes, usando esta só de
# fallback. Marca-segura ("nossa equipe", nunca "Dr. Mario").
_MSG_HANDOFF_LEAD = (
    "Vou pedir pra alguém da nossa equipe continuar seu atendimento por "
    "aqui, tá? Já já te respondem. 🙏"
)
# Remove placeholder cru ({{HORARIOS}}, {{MEET_LINK}}, ...) de uma mensagem
# antes de mandar pro lead — usado no handoff, único send sem replace dedicado.
_RE_PLACEHOLDER = re.compile(r"\s*\{\{[^}]+\}\}")


async def _handle_oferecer_horarios(
    *,
    conn: Any,
    lead: dict[str, Any],
    decisao: Decisao,
    transcript: str,
    new_hash: str,
    jurichat: JurichatClient,
    calendar: CalendarConfig,
    mario_conversation_id: str,
    poll_interval_seconds: int,
    exigir_email: bool = True,
) -> None:
    """Busca slots reais, substitui ``{{HORARIOS}}``, envia.

    ``exigir_email=False`` pula o email-gate (G2) — usado na remarcação, onde o
    lead JÁ tem uma reunião (logo já deu o email), então re-pedir seria bobo.
    """
    lead_id = lead["id"]
    conv_id = lead["jurichat_conversation_id"]

    if calendar.client is None:
        # Sem Google configurado — degradar pra handoff.
        await _handoff_sem_calendar(
            conn=conn, lead=lead, transcript=transcript, new_hash=new_hash,
            jurichat=jurichat, mario_conversation_id=mario_conversation_id,
            motivo="calendar_nao_configurado",
        )
        return

    # G2 (auditoria 24/jun): email-gate. O Meet precisa do email do lead pro
    # convite, e o fluxo da skill é "pede email → oferece horários". Se o modelo
    # pulou pra oferecer_horarios SEM email na transcrição, pede o email primeiro
    # em vez de mostrar 4 slots e só descobrir no confirmar que falta email.
    # Espelha o guardrail do _handle_confirmar_horario (o gate existia só no
    # prompt; agora é revalidado no handler). Remarcação pula (exigir_email=False).
    if exigir_email and _extrair_email(transcript) is None:
        msg_email = (
            "Pra eu te enviar o convite com o link da videochamada (Google "
            "Meet), qual seu melhor email?"
        )
        try:
            await jurichat.start_human_support(conv_id)
            await jurichat.send_message(conv_id, msg_email)
        except Exception as exc:
            logger.exception(
                "send_message(oferecer_pede_email) lead=%s: %s", lead_id, exc,
            )
            register_error(conn, lead_id, "jurichat_send_failed")
        update_transcript_hash(conn, lead_id, new_hash)
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return

    # G1 (2026-06-16): re-oferta NÃO repete horário já oferecido (e recusado).
    # Acumula o histórico de oferecidos e exclui da nova geração — o lead
    # sempre vê horários NOVOS. Se esgotar, find_available_slots devolve []
    # → handoff avisado abaixo.
    ja_oferecidos = get_horarios_oferecidos(conn, lead_id)
    exclude_isos = {o["iso"] for o in ja_oferecidos}
    try:
        slots = await calendar.client.find_available_slots(
            business_hours_start=calendar.business_hours_start,
            business_hours_end=calendar.business_hours_end,
            slot_min=calendar.slot_min,
            buffer_min=calendar.buffer_min,
            lookahead_days=calendar.lookahead_days,
            num_slots=calendar.num_slots,
            morning_start=calendar.morning_start,
            morning_end=calendar.morning_end,
            exclude_isos=exclude_isos,
        )
    except (GoogleCalendarError, httpx.HTTPError) as exc:
        # Falha TRANSITÓRIA — erro do Google (GoogleCalendarError) OU de rede/
        # HTTP cru (timeout/5xx/transport: o find_available_slots NÃO os envolve
        # em GoogleCalendarError, então vazam como httpx) → re-tenta no próximo
        # tick. Se persistir, o sweep F1 alerta o Mario após N seguidas.
        logger.warning(
            "find_available_slots (calendar) falhou lead=%s: %s", lead_id, exc,
        )
        register_error(conn, lead_id, "calendar_find_slots_failed")
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return
    except Exception as exc:
        # F2 (auditoria 24/jun): erro INESPERADO (nem Google, nem rede/HTTP) =
        # provável bug determinístico (TypeError/KeyError/ValueError — ex.:
        # regressão pós-deploy, payload mudou). Antes caía no mesmo reschedule
        # mudo → loop infinito mascarando a regressão. Agora degrada pra handoff
        # (que JÁ alerta o Mario via notify_mario) em vez de re-tentar o bug.
        logger.exception(
            "find_available_slots erro INESPERADO lead=%s: %s", lead_id, exc,
        )
        register_error(conn, lead_id, "calendar_find_slots_erro_inesperado")
        await _handoff_sem_calendar(
            conn=conn, lead=lead, transcript=transcript, new_hash=new_hash,
            jurichat=jurichat, mario_conversation_id=mario_conversation_id,
            motivo="calendar_find_slots_erro_inesperado",
        )
        return

    if not slots:
        # Agenda lotada — degradar pra handoff humano.
        await _handoff_sem_calendar(
            conn=conn, lead=lead, transcript=transcript, new_hash=new_hash,
            jurichat=jurichat, mario_conversation_id=mario_conversation_id,
            motivo="agenda_lotada_proximos_dias",
        )
        return

    horarios_texto = _format_slots_human(slots)
    if "{{HORARIOS}}" in decisao.mensagem:
        mensagem = decisao.mensagem.replace("{{HORARIOS}}", horarios_texto)
    else:
        # Claude esqueceu o placeholder — anexa no final.
        mensagem = f"{decisao.mensagem.rstrip()}\n\n{horarios_texto}"

    try:
        await jurichat.start_human_support(conv_id)
        await jurichat.send_message(conv_id, mensagem)
    except Exception as exc:
        logger.exception(
            "send_message(oferecer_horarios) failed for lead=%s: %s",
            lead_id, exc,
        )
        register_error(conn, lead_id, "jurichat_send_failed")
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return

    # Persiste os horários oferecidos pra escolha DETERMINÍSTICA no próximo
    # turno (Signal 1.8) — não depende do Claude pra confirmar (bugfix Camila).
    # ACUMULA com os já oferecidos (G1): o Signal 1.8 casa qualquer slot já
    # ofertado, e a próxima re-oferta exclui todos eles. De-dup por iso +
    # teto defensivo (o acúmulo já é limitado por max_turnos + esgotamento).
    combinados = ja_oferecidos + [
        {"iso": s.start.isoformat(), "label": s.format_human()} for s in slots
    ]
    vistos: set[str] = set()
    unicos: list[dict] = []
    for o in combinados:
        if o["iso"] not in vistos:
            vistos.add(o["iso"])
            unicos.append(o)
    set_horarios_oferecidos(conn, lead_id, unicos[-40:])
    update_transcript_hash(conn, lead_id, new_hash)
    schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)


async def _handle_confirmar_horario(
    *,
    conn: Any,
    lead: dict[str, Any],
    decisao: Decisao,
    transcript: str,
    new_hash: str,
    jurichat: JurichatClient,
    calendar: CalendarConfig,
    mario_conversation_id: str,
    poll_interval_seconds: int,
    juridiq: JuridiqClient | None = None,
) -> None:
    """Valida horário escolhido, cria evento, handoff, notifica Mario."""
    lead_id = lead["id"]
    conv_id = lead["jurichat_conversation_id"]

    if calendar.client is None:
        await _handoff_sem_calendar(
            conn=conn, lead=lead, transcript=transcript, new_hash=new_hash,
            jurichat=jurichat, mario_conversation_id=mario_conversation_id,
            motivo="calendar_nao_configurado",
        )
        return

    iso = decisao.horario_escolhido_iso
    if not iso:
        logger.error(
            "confirmar_horario sem horario_escolhido_iso (lead=%s)", lead_id,
        )
        register_error(conn, lead_id, "claude_horario_iso_ausente")
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return

    # Guardrail: skill exige email pra criar Meet. Se Claude esqueceu de
    # pedir/incluir, força pedir agora (mensagem direta pro lead) e
    # bloqueia create_event. Próximo tick processa a resposta do lead.
    if not decisao.lead_email or "@" not in decisao.lead_email:
        logger.warning(
            "confirmar_horario sem lead_email válido (lead=%s value=%r) — "
            "forçando pedido de email", lead_id, decisao.lead_email,
        )
        msg = (
            "Antes de confirmar, qual seu melhor email? Vou te enviar o "
            "convite com o link da videochamada (Google Meet)."
        )
        try:
            await jurichat.start_human_support(conv_id)
            await jurichat.send_message(conv_id, msg)
        except Exception as exc:
            logger.exception(
                "send_message(pedir_email_guardrail) failed lead=%s: %s",
                lead_id, exc,
            )
        update_transcript_hash(conn, lead_id, new_hash)
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return

    start, erro = _parse_horario_confirmado(iso, calendar.timezone)
    if erro == "horario_no_passado":
        # Claude extraiu data velha da transcrição (conversa retomada
        # dias depois). Pede pro lead re-escolher em vez de criar
        # evento no passado.
        logger.warning(
            "horario confirmado no PASSADO %r (lead=%s) — pedindo novo",
            iso, lead_id,
        )
        register_error(conn, lead_id, "horario_no_passado")
        # S5 (16/jun): os slots oferecidos venceram junto — limpa pra não
        # perpetuar o loop "já passou" (lead re-escolhe → casa de novo o slot
        # morto). Mensagem direciona à agenda ATUALIZADA, não "um dos que te
        # mandei" (que estão todos vencidos).
        clear_horarios_oferecidos(conn, lead_id)
        msg = (
            "Esse horário já passou! Vou te mostrar a agenda atualizada com "
            "os próximos horários disponíveis."
        )
        try:
            await jurichat.start_human_support(conv_id)
            await jurichat.send_message(conv_id, msg)
        except Exception as exc:
            logger.exception(
                "send_message(horario_passado) failed lead=%s: %s",
                lead_id, exc,
            )
        update_transcript_hash(conn, lead_id, new_hash)
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return
    if start is None:
        logger.error(
            "horario_escolhido_iso inválido %r (lead=%s)", iso, lead_id,
        )
        register_error(conn, lead_id, "claude_horario_iso_invalido")
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return

    # ISO normalizado (aware, com offset) — fonte única pro check D2, pro
    # set_reuniao (D1) e pro payload da transição. Nunca o cru do LLM.
    iso_normalizado = start.isoformat()

    # D2 (auditoria 24/jun): barra double-booking. O find_available_slots só lê
    # o freeBusy do Calendar (eventual-consistente), nunca o DB — dois leads
    # conseguiriam confirmar o MESMO slot. Checa o DB antes de criar o evento:
    # se outro lead já tem reunião nesse horário, reoferece a agenda atualizada
    # SEM tocar na reunião atual deste lead (remarcação não perde o slot velho).
    if lead_com_reuniao_no_horario(conn, iso_normalizado, lead_id) is not None:
        logger.warning(
            "double-booking barrado: horario %s já ocupado por outro lead "
            "(lead=%s) — reoferecendo", iso_normalizado, lead_id,
        )
        register_error(conn, lead_id, "double_booking_barrado")
        clear_horarios_oferecidos(conn, lead_id)
        msg = (
            "Ihh, esse horário acabou de ser preenchido! Vou te mostrar a "
            "agenda atualizada com os próximos horários disponíveis."
        )
        try:
            await jurichat.start_human_support(conv_id)
            await jurichat.send_message(conv_id, msg)
        except Exception as exc:
            logger.exception(
                "send_message(double_booking) failed lead=%s: %s", lead_id, exc,
            )
        update_transcript_hash(conn, lead_id, new_hash)
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
        return

    # Lead JÁ TINHA reunião marcada e confirmou outra (remarcação que
    # o Claude rotulou de confirmar em vez de remarcar): cancela o
    # evento antigo antes de criar o novo — senão Mario fica com
    # double-booking e o lead com 2 convites (auditoria 2026-06-10).
    evento_antigo = lead["reuniao_event_id"]
    evento_antigo_cancelado = False
    if evento_antigo:
        try:
            await calendar.client.cancel_event(evento_antigo)
            evento_antigo_cancelado = True
            logger.info(
                "lead=%s: evento antigo %s cancelado antes do novo",
                lead_id, evento_antigo,
            )
        except Exception as exc:
            logger.warning(
                "cancel evento antigo %s falhou (lead=%s): %s — segue",
                evento_antigo, lead_id, exc,
            )

    # S4 (idempotência, 16/jun): consome a escolha e marca o transcript como
    # processado ANTES do create_event. Se o processo morrer logo após o
    # create, o próximo start não re-casa (Signal 1.8 vê oferecidos vazio) nem
    # reprocessa o mesmo transcript (hash já igual) → sem reunião duplicada.
    # Vem DEPOIS dos guardrails de email/horario-no-passado (que dependem dos
    # slots/hash ainda existirem pra reprocessar a resposta do lead).
    clear_horarios_oferecidos(conn, lead_id)
    update_transcript_hash(conn, lead_id, new_hash)

    # Criar o evento (a API do Google rejeita conflito hard se houver,
    # mas como freeBusy é eventualmente consistente, não validamos
    # antes — confiamos no create. Se der erro 409/4xx, log e degradar.)
    meet_link = ""
    event_id = ""
    try:
        event = await calendar.client.create_event(
            start=start,
            duration_min=calendar.slot_min,
            lead_nome=lead["contato_nome"] or "Lead",
            lead_telefone=lead["contato_telefone"] or "?",
            resumo_caso=decisao.resumo_caso or "(sem resumo do bot)",
            lead_email=decisao.lead_email,
        )
        # hangoutLink só vem se conferenceData foi pedido (i.e., havia
        # lead_email). Vazio é OK — o template substitui sem quebrar.
        meet_link = event.get("hangoutLink", "") or ""
        event_id = event.get("id", "") or ""
    except Exception as exc:
        logger.exception(
            "create_event failed for lead=%s: %s", lead_id, exc,
        )
        register_error(conn, lead_id, "calendar_create_event_failed")
        # S7 (16/jun): se cancelamos o evento antigo e o create falhou, o DB
        # ficaria apontando pra um evento CANCELADO (reminder_cycle dispararia
        # lembretes fantasma). Limpa a reunião antes do handoff.
        if evento_antigo_cancelado:
            clear_reuniao(conn, lead_id)
        # Degradação: avisa lead, notifica Mario, handoff manual.
        await _handoff_sem_calendar(
            conn=conn, lead=lead, transcript=transcript, new_hash=new_hash,
            jurichat=jurichat, mario_conversation_id=mario_conversation_id,
            motivo="falha_criar_evento_calendar",
        )
        return

    # D1 (auditoria 24/jun): grava a reunião no DB IMEDIATAMENTE após o
    # create_event, ANTES dos passos fire-and-forget (mensagem/send/intake/
    # notify). Se o processo morrer no meio (deploy/OOM/restart do scheduler),
    # o evento já existe no Google E o reuniao_em já está persistido → os
    # lembretes 24h/2h/30min saem. Antes o set_reuniao vinha DEPOIS do send,
    # abrindo a janela do evento órfão sem lembrete (no-show silencioso).
    # iso_normalizado já foi calculado acima (fonte única, usada no check D2).
    set_reuniao(
        conn, lead_id,
        reuniao_em_iso=iso_normalizado, event_id=event_id, meet_link=meet_link,
    )

    # Substitui placeholders no texto de confirmação. Converte pro tz
    # da agenda ANTES de formatar — o LLM pode mandar offset de outro
    # fuso e o lead leria a hora errada.
    from zoneinfo import ZoneInfo
    start_local = start.astimezone(ZoneInfo(calendar.timezone))
    horario_humano = Slot(
        start=start_local, duration_min=calendar.slot_min,
    ).format_human()
    mensagem = decisao.mensagem
    if "{{HORARIO_CONFIRMADO}}" in mensagem:
        mensagem = mensagem.replace("{{HORARIO_CONFIRMADO}}", horario_humano)
    else:
        mensagem = f"{mensagem.rstrip()}\n\n(agendado pra {horario_humano})"
    # {{MEET_LINK}}: se calendar não criou Meet (sem email), remove o
    # placeholder pra não vazar literal no WhatsApp.
    if "{{MEET_LINK}}" in mensagem:
        if meet_link:
            mensagem = mensagem.replace("{{MEET_LINK}}", meet_link)
        else:
            # Sem Meet — remove a frase do template ("{{MEET_LINK}}" + qualquer
            # texto solto na mesma linha vira nada).
            mensagem = mensagem.replace("{{MEET_LINK}}", "").rstrip()

    confirmacao_enviada = True
    try:
        await jurichat.start_human_support(conv_id)
        await jurichat.send_message(conv_id, mensagem)
    except Exception as exc:
        # Evento já criado — segue o fluxo, mas o lead NÃO recebeu a
        # confirmação/Meet link. O notify abaixo avisa o Mario pra
        # confirmar manualmente (auditoria 2026-06-11: antes a falha
        # era engolida em silêncio).
        confirmacao_enviada = False
        register_error(conn, lead_id, "jurichat_send_failed_confirmacao")
        logger.exception(
            "send_message(confirmar_horario) failed for lead=%s: %s",
            lead_id, exc,
        )

    # set_reuniao já rodou logo após o create_event (D1) — o lead PERMANECE
    # em_conversa pra processar resposta a um lembrete (ex: "preciso remarcar").
    # clear_horarios_oferecidos + update_transcript_hash já rodaram ANTES do
    # create_event (S4, idempotência). Aqui só reagenda o próximo poll.
    schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
    # Registra a "transição" no log de auditoria sem mudar estado.
    transicao(
        conn, lead_id, Estado.EM_CONVERSA,
        motivo="claude_confirmar_horario",
        payload={
            "horario_iso": iso_normalizado,
            "event_id": event_id,
            "meet_link": meet_link,
            "resumo_caso": decisao.resumo_caso,
        },
    )

    # Intake Juridiq (2026-06-10): lead agendou → cria a Pessoa no
    # Juridiq com a qualificação completa. Fire-and-forget — o helper
    # nunca levanta; falha vira log e o agendamento segue intacto.
    juridiq_person_id: str | None = None
    if juridiq is not None:
        juridiq_person_id = await intake_lead_agendado(
            juridiq,
            nome=lead["contato_nome"] or "Lead sem nome",
            telefone=lead["contato_telefone"] or "",
            email=decisao.lead_email,
            resumo_caso=decisao.resumo_caso or "(sem resumo)",
            horario_humano=horario_humano,
            meet_link=meet_link,
        )

    try:
        notify_text = (
            f"📅 *Agendado via bot*\n\n"
            f"Lead: {lead['contato_nome']}\n"
            f"Tel: {lead['contato_telefone']}\n"
        )
        if decisao.lead_email:
            notify_text += f"Email: {decisao.lead_email}\n"
        notify_text += (
            f"Quando: {horario_humano}\n\n"
            f"Resumo: {decisao.resumo_caso or '(sem resumo)'}\n\n"
            f"Evento já criado no seu Google Calendar."
        )
        if meet_link:
            notify_text += f"\nMeet: {meet_link}"
        if juridiq_person_id:
            notify_text += "\nFicha criada no Juridiq ✅"
        if not confirmacao_enviada:
            notify_text += (
                "\n\n⚠️ ATENÇÃO: a confirmação NÃO chegou no WhatsApp "
                "do lead (falha de envio). Confirme com ele manualmente."
            )
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=notify_text,
        )
    except Exception as exc:
        logger.exception(
            "notify_mario(agendado) failed for lead=%s: %s", lead_id, exc,
        )


async def _handle_remarcar_reuniao(
    *,
    conn: Any,
    lead: dict[str, Any],
    decisao: Decisao,
    transcript: str,
    new_hash: str,
    jurichat: JurichatClient,
    calendar: CalendarConfig,
    mario_conversation_id: str,
    poll_interval_seconds: int,
) -> None:
    """Cancela evento atual, oferece novos horários."""
    lead_id = lead["id"]

    event_id = lead["reuniao_event_id"]
    # Sempre tenta cancelar o evento se temos id+client. Sem id, segue
    # direto pra oferecer novos horários (talvez lead pediu remarcar sem
    # ter agendado — Claude se confundiu).
    if calendar.client is not None and event_id:
        try:
            await calendar.client.cancel_event(event_id)
        except Exception as exc:
            logger.warning(
                "cancel_event failed for lead=%s event=%s: %s — "
                "seguindo mesmo assim", lead_id, event_id, exc,
            )

    # Limpa reunião do DB (libera flags de lembrete + remove referência).
    clear_reuniao(conn, lead_id)

    # Notifica Mario do cancelamento.
    try:
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=(
                f"🔁 *Lead pediu remarcação*\n\n"
                f"Lead: {lead['contato_nome']}\n"
                f"Tel: {lead['contato_telefone']}\n\n"
                f"Cancelei o evento anterior no Calendar. Bot já está "
                f"oferecendo novos horários."
            ),
        )
    except Exception as exc:
        logger.exception(
            "notify_mario(remarcar) failed lead=%s: %s", lead_id, exc,
        )

    # Reusa exatamente o handler de oferecer_horarios pra mandar slots
    # novos (substitui {{HORARIOS}} na mensagem do Claude). exigir_email=False
    # (G2): quem remarca já tem reunião → já deu o email; não re-pede.
    await _handle_oferecer_horarios(
        conn=conn, lead=lead, decisao=decisao,
        transcript=transcript, new_hash=new_hash,
        jurichat=jurichat, calendar=calendar,
        mario_conversation_id=mario_conversation_id,
        poll_interval_seconds=poll_interval_seconds,
        exigir_email=False,
    )


async def _handle_cancelar_reuniao(
    *,
    conn: Any,
    lead: dict[str, Any],
    decisao: Decisao,
    new_hash: str,
    jurichat: JurichatClient,
    calendar: CalendarConfig,
    mario_conversation_id: str,
    poll_interval_seconds: int,
) -> None:
    """Lead DESMARCOU sem remarcar (pedido Mario 2026-06-10).

    Diferente de remarcar: não oferece novos horários. Cancela o evento
    no Calendar, limpa a reunião, envia a confirmação do Claude ao lead
    e — o ponto do pedido — AVISA O MARIO IMEDIATAMENTE no WhatsApp.

    Lead segue em_conversa (pode voltar a marcar depois).
    """
    lead_id = lead["id"]
    conv_id = lead["jurichat_conversation_id"]

    horario_humano = ""
    if lead["reuniao_em"]:
        try:
            reuniao_dt = datetime.datetime.fromisoformat(lead["reuniao_em"])
            horario_humano = _format_reuniao_human(
                reuniao_dt.astimezone(datetime.UTC)
            )
        except (ValueError, TypeError):
            horario_humano = lead["reuniao_em"]

    event_id = lead["reuniao_event_id"]
    if calendar.client is not None and event_id:
        try:
            await calendar.client.cancel_event(event_id)
        except Exception as exc:
            logger.warning(
                "cancel_event(desmarcar) lead=%s event=%s: %s — segue",
                lead_id, event_id, exc,
            )

    clear_reuniao(conn, lead_id)
    clear_horarios_oferecidos(conn, lead_id)

    # Confirmação pro lead (mensagem do Claude, sem placeholders).
    try:
        await jurichat.start_human_support(conv_id)
        await jurichat.send_message(conv_id, decisao.mensagem)
    except Exception as exc:
        logger.exception(
            "send_message(cancelar) failed lead=%s: %s", lead_id, exc,
        )

    schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
    update_transcript_hash(conn, lead_id, new_hash)
    transicao(
        conn, lead_id, Estado.EM_CONVERSA,
        motivo="claude_cancelar_reuniao",
        payload={"horario_cancelado": lead["reuniao_em"]},
    )

    # AVISO IMEDIATO PRO MARIO.
    try:
        quando = f" de {horario_humano}" if horario_humano else ""
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=(
                f"❌ *Reunião DESMARCADA pelo lead*\n\n"
                f"Lead: {lead['contato_nome']}\n"
                f"Tel: {lead['contato_telefone']}\n"
                f"Reunião{quando} foi cancelada.\n\n"
                f"Evento removido do seu Calendar. O lead não pediu "
                f"novo horário — segue em atendimento."
            ),
        )
    except Exception as exc:
        logger.exception(
            "notify_mario(desmarcar) failed lead=%s: %s", lead_id, exc,
        )


async def _handoff_sem_calendar(
    *,
    conn: Any,
    lead: dict[str, Any],
    transcript: str,
    new_hash: str,
    jurichat: JurichatClient,
    mario_conversation_id: str,
    motivo: str,
) -> None:
    """Fallback comum: calendar fora do ar / agenda lotada / erro create.

    Avisa lead, transiciona pra AGUARDANDO_HUMANO, pinga Mario.
    """
    lead_id = lead["id"]
    conv_id = lead["jurichat_conversation_id"]
    msg_lead = (
        "Vou te conectar com o Mario aqui mesmo pra confirmar o horário. "
        "Em instantes ele responde."
    )
    try:
        await jurichat.start_human_support(conv_id)
        await jurichat.send_message(conv_id, msg_lead)
    except Exception as exc:
        logger.exception(
            "send_message(handoff_calendar) failed for lead=%s: %s",
            lead_id, exc,
        )

    transicao(
        conn, lead_id, Estado.AGUARDANDO_HUMANO,
        motivo=motivo,
        proxima_acao_horas=CLEAR_PROXIMA_ACAO,
    )
    # S6 (16/jun): caminho terminal pra humano — não deixa slots órfãos
    # pendentes (idempotente em quem não tem).
    clear_horarios_oferecidos(conn, lead_id)
    update_transcript_hash(conn, lead_id, new_hash)

    try:
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=format_notification(
                tipo="handoff",
                nome=lead["contato_nome"],
                telefone=lead["contato_telefone"],
                ultima_msg=_last_lead_message(transcript),
                motivo=f"pedido agendamento — {motivo}",
                conversation_id=conv_id,
            ),
        )
    except Exception as exc:
        logger.exception(
            "notify_mario(handoff_calendar) failed for lead=%s: %s",
            lead_id, exc,
        )


# F1 (auditoria 24/jun): falhas CONSECUTIVAS antes de o poll cycle alertar o
# Mario sobre um lead preso em falha de API recorrente.
_ALERTA_ERRO_CONSECUTIVO = 3


async def _alertar_leads_presos(
    conn: Any,
    jurichat: JurichatClient,
    mario_conversation_id: str,
) -> None:
    """F1 (auditoria 24/jun): avisa o Mario UMA vez sobre cada lead preso em
    falha recorrente (erro_consecutivo >= limiar, ainda não alertado). Antes
    erro_atual era write-only e um lead travado em erro de API ficava mudo dias
    sem o Mario saber. O bot NÃO pausa o lead — segue tentando (a falha pode ser
    transitória); só dá visibilidade. Espelha D3/D5: só carimba erro_alertado_em
    se o aviso saiu (ou se não há Mario configurado) → falha re-tenta no ciclo
    seguinte.
    """
    for lead in list_leads_presos(conn, _ALERTA_ERRO_CONSECUTIVO):
        enviado = await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=(
                "⚠️ Lead possivelmente PRESO em falha recorrente "
                f"({lead['erro_consecutivo']}x seguidas): "
                f"{lead['contato_nome'] or '?'} "
                f"({lead['contato_telefone'] or '?'}). "
                f"Último erro: {lead['erro_atual'] or '?'}. "
                "O bot segue tentando, mas dá uma olhada."
            ),
        )
        if enviado or not mario_conversation_id:
            marcar_erro_alertado(conn, lead["id"])


async def _bloquear_promessa_resultado(
    *,
    conn: Any,
    lead: dict[str, Any],
    decisao: Decisao,
    transcript: str,
    new_hash: str,
    jurichat: JurichatClient,
    mario_conversation_id: str,
) -> None:
    """E3 (auditoria 24/jun): a resposta do modelo tropeçou no filtro de promessa
    de resultado (OAB Prov. 205/2021). NÃO manda o texto do modelo ao lead —
    envia uma mensagem neutra, passa pro humano (AGUARDANDO_HUMANO, reabrível) e
    alerta o Mario com o trecho ofensivo pra ele assumir. Decisão do Mario
    (24/jun): bloquear + msg neutra + handoff, em vez de só monitorar.
    """
    lead_id = lead["id"]
    conv_id = lead["jurichat_conversation_id"]
    register_error(conn, lead_id, "promessa_resultado_bloqueada")
    logger.warning(
        "E3: resposta do bot bloqueada (promessa de resultado) lead=%s: %r",
        lead_id, decisao.mensagem[:120],
    )
    msg_lead = (
        "Deixa eu confirmar esse ponto com a equipe e já te retorno, tá? 🙏"
    )
    try:
        await jurichat.start_human_support(conv_id)
        await jurichat.send_message(conv_id, msg_lead)
    except Exception as exc:
        logger.exception(
            "send_message(promessa_bloqueada) failed for lead=%s: %s",
            lead_id, exc,
        )
    transicao(
        conn, lead_id, Estado.AGUARDANDO_HUMANO,
        motivo="promessa_resultado_handoff",
        proxima_acao_horas=CLEAR_PROXIMA_ACAO,
    )
    clear_horarios_oferecidos(conn, lead_id)
    update_transcript_hash(conn, lead_id, new_hash)
    try:
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=format_notification(
                tipo="handoff",
                nome=lead["contato_nome"],
                telefone=lead["contato_telefone"],
                ultima_msg=_last_lead_message(transcript),
                motivo=(
                    "BLOQUEIO OAB (promessa de resultado, Prov. 205/2021) — a "
                    "resposta do bot foi SEGURADA, não foi ao lead. Assuma a "
                    f'conversa. Trecho barrado: "{decisao.mensagem[:180]}"'
                ),
                conversation_id=conv_id,
            ),
        )
    except Exception as exc:
        logger.exception(
            "notify_mario(promessa_bloqueada) failed for lead=%s: %s",
            lead_id, exc,
        )


# Tags que indicam que a conversa NÃO deve ser atendida pelo bot
# (cliente já existente, advogado já lidando, lead desqualificado, etc.).
EXCLUDED_TAGS_FOR_BOT = frozenset({
    "Cliente Ativo",
    "Pagamento pendente",
    "Reunião marcada",
    "Advogado adverso",
    "Desqualificado",
})

# Motivos de transição para AGUARDANDO_HUMANO que são TERMINAIS — o lead NÃO
# deve ser reaberto pelo bot mesmo que volte a falar (P1 auditoria 24/jun):
# pediu pra parar (opt_out), humano assumiu pelo painel (Signal 0), é o canal
# de alertas, ou não é lead de funil (baseline/filtros). Qualquer outro motivo
# (max_turnos, claude_handoff/propor, falhas de calendar, atendimento_processo)
# é REABRÍVEL: bot único → re-engajar é melhor que ghostar.
_MOTIVOS_AH_TERMINAIS = frozenset({
    "opt_out",
    "humano_assumiu_conversa",
    "canal_alertas_mario",
    "baseline_first_sync",
    "filtro_tem_responsavel",
    "filtro_tag_exclusao",
    "excluido_followup_etiqueta",
})

# H2 (auditoria 24/jun): máx. de leads AGUARDANDO_HUMANO checados por tick na
# sweep de re-engaje. Round-robin via ah_checado_em → trabalho LIMITADO por tick
# (em vez de O(AH) chamadas get_conversation), todo AH coberto em ~ceil(AH/N)
# ticks. Re-engaje de um lead que voltou a falar atrasa no máximo esses ticks.
_AH_SWEEP_LIMIT = 25


async def sync_jurichat_conversations(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    inbox_id: str,
    mario_conversation_id: str = "",
) -> dict[str, int]:
    """Sincroniza conversas Jurichat → leads no nosso DB.

    Por que existe: Jurichat NÃO emite webhook por mensagem nova
    (confirmado 2026-06-07). ``chat.conversation.updated`` só dispara
    em mudança de status (atribuição a bot, encerramento, etc.).

    Estratégia defensiva (combinada):

    1. **Primeira execução** (DB de leads vazio): registra TODAS as
       conversas existentes como AGUARDANDO_HUMANO. Bot não atende
       essas — funciona como baseline pra não bagunçar conversas
       reais que já estão rodando.

    2. **Execuções subsequentes**: pra cada conversa nova (que não
       está no nosso DB), aplica 2 filtros:
       a) Pula se tem ``responsables`` (advogado atribuído).
       b) Pula se tem tag em ``EXCLUDED_TAGS_FOR_BOT``.
       Se passar nos 2: registra como em_conversa + agenda poll
       imediato.

    Retorna ``{"baseline": N, "novos": N, "ignoradas": N}`` pra log.
    """
    conn = get_db()
    try:
        conversations = await jurichat.list_active_conversations(
            inbox_id=inbox_id,
        )
    except Exception as exc:
        logger.exception(
            "sync_jurichat_conversations: list_active_conversations falhou: %s",
            exc,
        )
        return {"baseline": 0, "novos": 0, "ignoradas": 0}

    is_first_sync = (
        conn.execute("SELECT COUNT(*) AS n FROM leads").fetchone()["n"] == 0
    )

    stats = {"baseline": 0, "novos": 0, "ignoradas": 0}
    canais_alertas = set(split_conversation_ids(mario_conversation_id))

    async def _alerta_lead_novo(person: dict, status: str) -> None:
        """🆕 pro canal do Mario em TODO lead novo (pedido 2026-06-12).

        Antes só o caminho "bot atende" alertava — lead que entrava com
        responsável ou tag de exclusão passava em silêncio. Fire-and-
        forget: falha de notificação nunca impede o processamento.
        """
        if not mario_conversation_id:
            return
        nome = person.get("name") or "(sem nome)"
        telefone = person.get("phoneNumber") or "?"
        try:
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=(
                    f"🆕 *Lead novo no funil*\n\n"
                    f"Nome: {nome}\n"
                    f"Tel: {telefone}\n\n"
                    f"{status}"
                ),
            )
        except Exception as exc:
            logger.exception(
                "notify_mario(lead_novo) falhou pra %s: %s", nome, exc,
            )

    for conv in conversations:
        # Pula arquivadas e grupos (lead individual é nosso caso).
        if conv.get("isArchived"):
            continue
        if conv.get("isGroup"):
            continue

        conv_id = conv.get("id")

        # Canais de alertas (Mario/equipe) — NUNCA tratar como lead.
        # Sem esse skip, o bot responderia as próprias notificações que
        # envia (ou qualificaria o Mario como lead de inventário 😅).
        if conv_id in canais_alertas:
            continue
        person = conv.get("person") or {}
        person_id = person.get("id")
        if not conv_id or not person_id:
            continue

        if get_lead_by_conversation(conn, conv_id) is not None:
            continue  # já conhecemos, poll cycle cuida

        # Cria o lead row (em em_conversa por default — vamos transicionar
        # imediatamente pra AGUARDANDO_HUMANO se não for elegível).
        create_lead_if_absent(
            conn,
            jurichat_lead_id=person_id,
            jurichat_conversation_id=conv_id,
            contato_telefone=person.get("phoneNumber", ""),
            contato_nome=person.get("name"),
        )
        lead = get_lead_by_conversation(conn, conv_id)
        if lead is None:
            continue

        # Filtro 0: primeira execução → tudo vira AGUARDANDO_HUMANO.
        if is_first_sync:
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="baseline_first_sync",
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            stats["baseline"] += 1
            continue

        # Filtro 1: já tem advogado atribuído → não atende.
        responsables = conv.get("responsables") or []
        if responsables:
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="filtro_tem_responsavel",
                payload={"responsables": responsables},
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            stats["ignoradas"] += 1
            await _alerta_lead_novo(
                person,
                "⚠️ Já entrou com responsável atribuído — o bot NÃO vai "
                "atender. Atendimento por sua conta.",
            )
            continue

        # Filtro 2: tem tag de exclusão → não atende.
        # (Custo extra: 1 chamada à API por lead novo elegível em
        # responsables-vazio. Tolerável em volume baixo.)
        try:
            tags = await jurichat.get_lead_tags(person_id)
        except Exception as exc:
            logger.warning(
                "get_lead_tags falhou pra person_id=%s: %s", person_id, exc,
            )
            tags = []

        if any(t in EXCLUDED_TAGS_FOR_BOT for t in tags):
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="filtro_tag_exclusao",
                payload={"tags": tags},
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            stats["ignoradas"] += 1
            await _alerta_lead_novo(
                person,
                f"⚠️ Tem etiqueta de exclusão ({', '.join(tags)}) — o bot "
                f"NÃO vai atender. Atendimento por sua conta.",
            )
            continue

        # Passou nos 2 filtros — bot atende.
        schedule_next_action_seconds(conn, lead["id"], 0)
        stats["novos"] += 1

        await _alerta_lead_novo(
            person,
            "O bot (Julia) já está atendendo. Você recebe novo alerta "
            "quando ele agendar ou pedir humano.",
        )

    if is_first_sync:
        logger.info(
            "sync_jurichat_conversations: PRIMEIRA EXECUCAO — %d conversas "
            "registradas como AGUARDANDO_HUMANO (baseline, bot nao atende)",
            stats["baseline"],
        )
    elif stats["novos"] > 0 or stats["ignoradas"] > 0:
        logger.info(
            "sync_jurichat_conversations: %d novos leads atendiveis, "
            "%d ignorados (responsavel ou tag)",
            stats["novos"], stats["ignoradas"],
        )
    return stats


async def run_poll_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    triagem_fn: Callable[..., Awaitable[Decisao]],
    mario_conversation_id: str,
    max_turnos: int,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
    calendar: CalendarConfig | None = None,
    bot_user_id: str = "",
    juridiq: JuridiqClient | None = None,
    datajud_api_key: str = "",
) -> None:
    """Process all em_conversa leads whose poll tick is due."""
    conn = get_db()

    # FASE 0 — REATIVAÇÃO (auditoria 2026-06-11, HIGH): leads em
    # FU1/FU2/encerrado que RESPONDERAM voltam pra em_conversa. Antes,
    # o polling era estritamente em_conversa: a resposta do lead ao
    # follow-up era invisível e ele ainda levava FU2 + encerramento.
    # Não atualizamos o hash na reativação — o tick seguinte detecta a
    # mudança e faz a triagem normal da mensagem nova.
    canais_alertas = set(split_conversation_ids(mario_conversation_id))
    for lead in list_leads_para_reativacao(conn):
        if lead["jurichat_conversation_id"] in canais_alertas:
            continue
        try:
            conv = await jurichat.get_conversation(
                lead["jurichat_conversation_id"]
            )
        except Exception as exc:
            logger.warning(
                "reativacao: get_conversation falhou lead=%s: %s",
                lead["id"], exc,
            )
            continue
        transcript = conv.get("transcription", "") or ""
        new_hash = _compute_hash(transcript)
        if new_hash == lead["ultimo_transcript_hash"]:
            continue
        if _last_line_from_atendente(transcript):
            # Mudança veio do NOSSO próprio follow-up (ou humano) —
            # registra o hash pra não re-checar, sem reativar.
            update_transcript_hash(conn, lead["id"], new_hash)
            continue
        logger.info(
            "lead=%s respondeu em estado %s — reativando pra em_conversa",
            lead["id"], lead["estado"],
        )
        mark_lead_activity_now(conn, lead["id"])
        # S6 (16/jun): qualquer oferta de horário anterior já venceu depois de
        # um ciclo de follow-up — limpa pra não re-disparar o Signal 1.8 sobre
        # um slot velho. O Claude reabre a conversa do zero.
        clear_horarios_oferecidos(conn, lead["id"])
        transicao(
            conn, lead["id"], Estado.EM_CONVERSA,
            motivo="lead_respondeu_reativacao",
        )
        # P0 (auditoria 24/jun): zera o teto — o histórico vitalício não pode
        # capar o lead reativado na 1a mensagem nova.
        reset_turnos(conn, lead["id"])
        schedule_next_action_seconds(conn, lead["id"], 0)

    # FASE 0.5 — RE-ENGAJE de AGUARDANDO_HUMANO (P1 auditoria 24/jun): o estado
    # era um buraco negro (bot é o ÚNICO atendimento → "handoff" virava "vácuo").
    # Se o lead em espera manda mensagem NOVA, reabre pro bot retomar + re-alerta
    # o Mario — EXCETO motivos terminais (opt-out, humano assumiu, não-lead).
    # H2 (auditoria 24/jun): só os N há mais tempo sem checar (round-robin via
    # ah_checado_em) → trabalho limitado por tick em vez de O(AH) get_conversation.
    for lead in list_leads_aguardando_humano(conn, limite=_AH_SWEEP_LIMIT):
        # Carimba JÁ (antes de qualquer continue) pra rotacionar pro fim da fila
        # — assim os outros AH entram nos próximos ticks. Re-engajado sai de AH,
        # então o carimbo é inócuo nesse caso.
        marcar_ah_checado(conn, lead["id"])
        conv_id_ah = lead["jurichat_conversation_id"]
        if conv_id_ah in canais_alertas:
            continue
        try:
            conv = await jurichat.get_conversation(conv_id_ah)
        except Exception as exc:
            logger.warning(
                "re-engaje AH: get_conversation falhou lead=%s: %s",
                lead["id"], exc,
            )
            continue
        transcript = conv.get("transcription", "") or ""
        new_hash = _compute_hash(transcript)
        if new_hash == lead["ultimo_transcript_hash"]:
            continue
        if _last_line_from_atendente(transcript):
            # Mudança veio do nosso lado (ou do humano que assumiu) — registra o
            # hash pra não re-checar, sem reabrir.
            update_transcript_hash(conn, lead["id"], new_hash)
            continue
        motivo_ah = ultimo_motivo_transicao(conn, lead["id"])
        if motivo_ah in _MOTIVOS_AH_TERMINAIS:
            # opt-out / humano assumiu / não-lead → fica mudo de propósito.
            update_transcript_hash(conn, lead["id"], new_hash)
            continue
        # Motivo reabrível → reabre pro bot + re-alerta o Mario. NÃO atualiza o
        # hash: o tick de polling seguinte faz a triagem da mensagem nova
        # (espelha a FASE 0 de reativação de FU/encerrado).
        logger.info(
            "lead=%s em AGUARDANDO_HUMANO respondeu (motivo=%s) — reabrindo",
            lead["id"], motivo_ah,
        )
        mark_lead_activity_now(conn, lead["id"])
        clear_horarios_oferecidos(conn, lead["id"])
        transicao(
            conn, lead["id"], Estado.EM_CONVERSA,
            motivo="lead_respondeu_em_espera",
        )
        reset_turnos(conn, lead["id"])
        schedule_next_action_seconds(conn, lead["id"], 0)
        try:
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=(
                    f"🔔 *Lead em espera voltou a falar*\n\n"
                    f"Lead: {lead['contato_nome']}\n"
                    f"Tel: {lead['contato_telefone']}\n\n"
                    f"Última: {_last_lead_message(transcript)}\n\n"
                    f"Estava em espera humana ({motivo_ah}). "
                    f"Reabri pro bot retomar — assuma se preferir."
                ),
            )
        except Exception as exc:
            logger.exception(
                "notify_mario(lead_voltou_espera) falhou lead=%s: %s",
                lead["id"], exc,
            )

    leads = list_leads_para_polling(conn)
    logger.info("poll tick: %d leads em_conversa due", len(leads))

    for lead in leads:
        lead_id = lead["id"]
        conv_id = lead["jurichat_conversation_id"]

        # Canal de alertas do Mario registrado como lead ANTES do
        # guardrail do sync existir (2026-06-10) — neutraliza de vez.
        # Sem isso, o bot tentaria qualificar o próprio Mario quando a
        # conversa de alertas tiver mensagem nova.
        if conv_id in canais_alertas:
            transicao(
                conn, lead_id, Estado.AGUARDANDO_HUMANO,
                motivo="canal_alertas_mario",
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            continue

        try:
            conv = await jurichat.get_conversation(conv_id)
        except Exception as exc:
            logger.exception(
                "poll get_conversation failed for lead=%s: %s", lead_id, exc,
            )
            register_error(conn, lead_id, "jurichat_get_conversation_failed")
            # Reschedule so we retry on the next tick — don't strand the lead.
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
            continue

        transcript = conv.get("transcription", "") or ""
        new_hash = _compute_hash(transcript)
        old_hash = lead["ultimo_transcript_hash"]

        # D4 (25/jun): se o LEAD mencionou um email, persiste (idempotente) pra
        # casar reuniões marcadas FORA do bot pelo email do convidado do evento.
        # _extrair_email_do_lead (não _extrair_email): só as falas do lead, de
        # baixo pra cima — senão gravaria o email do atendente/assinatura ou de
        # outro cliente citado, e a reunião manual auto-vincularia ao lead ERRADO
        # (revisão adversarial 25/jun).
        set_lead_email(conn, lead_id, _extrair_email_do_lead(transcript))

        # Signal 0 (2026-06-10): atendente HUMANO assumiu a conversa.
        # O campo ``user`` da conversa identifica o responsável atual.
        # Quando o bot tem identidade própria (bot_user_id setado via
        # JURICHAT_BOT_USER_ID) e o responsável é OUTRO usuário, um
        # humano assumiu pelo painel → bot se pausa na hora, antes de
        # qualquer chamada ao Claude. Roda ANTES do hash-check: mesmo
        # sem mensagem nova, conversa assumida sai do polling.
        conv_user = conv.get("user") or {}
        conv_user_id = conv_user.get("id") if isinstance(conv_user, dict) else None
        if bot_user_id and conv_user_id and conv_user_id != bot_user_id:
            logger.info(
                "lead=%s: humano %r assumiu a conversa — bot pausado",
                lead_id, conv_user.get("name"),
            )
            clear_horarios_oferecidos(conn, lead_id)  # S6 (16/jun)
            transicao(
                conn, lead_id, Estado.AGUARDANDO_HUMANO,
                motivo="humano_assumiu_conversa",
                payload={"user": dict(conv_user)},
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            update_transcript_hash(conn, lead_id, new_hash)
            continue

        # Nothing new since the last poll → just reschedule.
        if new_hash == old_hash:
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
            continue

        # New content detected — mark lead as freshly active so the
        # follow-up cycle's "idle > 24h" carve-out keeps its hands off.
        mark_lead_activity_now(conn, lead_id)

        # Signal 1: última mensagem é OUTBOUND (atendente) — pode ser
        # nossa própria resposta OU humano real assumindo. Como não dá
        # pra distinguir sem trackear messageIds individuais, adotamos a
        # política DEFENSIVA: NÃO marca como AGUARDANDO_HUMANO. Só
        # atualiza o hash e reschedule. Se o lead responder depois, o
        # próximo tick processa.
        #
        # Trade-off conhecido: se humano real responder pelo Jurichat
        # web, o bot pode continuar tentando responder no próximo turno
        # do lead. Pra parar manualmente, mude o estado do lead no DB
        # ou adicione tag de exclusão na conversa.
        if _last_line_from_atendente(transcript):
            update_transcript_hash(conn, lead_id, new_hash)
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
            continue

        # Signal 1.4: OPT-OUT (LGPD, roadmap 1.10) — PRIMEIRO, antes de
        # qualquer alerta de relacionamento. Lead pediu pra parar → registra
        # na supressão, confirma sóbrio e SAI do funil. Vir antes garante que
        # nenhum alerta (cliente/conflito/urgência) contradiga a supressão no
        # mesmo tick (bug revisão 15/jun). try/except: falha não trava o ciclo.
        if detectar_opt_out(_last_lead_message(transcript)):
            try:
                registrar_opt_out(
                    conn, telefone=lead["contato_telefone"],
                    motivo="pediu no WhatsApp",
                )
                logger.info("lead=%s pediu opt-out — suprimindo", lead_id)
                clear_horarios_oferecidos(conn, lead_id)  # S6 (16/jun)
                transicao(
                    conn, lead_id, Estado.AGUARDANDO_HUMANO, motivo="opt_out",
                    proxima_acao_horas=CLEAR_PROXIMA_ACAO,
                )
                update_transcript_hash(conn, lead_id, new_hash)
                await jurichat.start_human_support(conv_id)
                await jurichat.send_message(
                    conv_id,
                    "Tudo bem, não vou mais te enviar mensagens. "
                    "Se um dia precisar, é só chamar por aqui. 🙏",
                )
            except Exception as exc:
                logger.exception(
                    "opt_out handling failed for lead=%s: %s", lead_id, exc,
                )
            continue

        # Signal 1.5: RECONHECER CLIENTE (1.6) + CONFLITO (1.7). Aditivos,
        # 1x por lead (cliente_checado_em). Cliente: cruza telefone com o
        # person_index. Conflito: nome bate com parte contrária → SÓ suspeita,
        # SÓ canal interno, NUNCA ao lead. try/except externo (bug revisão
        # 15/jun): falha local (DB/índice) não derruba o poll cycle dos
        # outros leads. mark_cliente_checado fora do try → não re-tenta em loop.
        if not lead["cliente_checado_em"]:
            mark_cliente_checado(conn, lead_id)
            try:
                ficha = resolver_telefone(conn, lead["contato_telefone"])
                if ficha:
                    logger.info(
                        "lead=%s reconhecido como cliente: %s",
                        lead_id, ficha.get("nome"),
                    )
                    await notify_mario(
                        jurichat,
                        mario_conversation_id=mario_conversation_id,
                        mensagem=format_notification(
                            tipo="cliente_retornou",
                            nome=ficha.get("nome") or lead["contato_nome"],
                            telefone=lead["contato_telefone"],
                            ultima_msg=_last_lead_message(transcript),
                            motivo=ficha.get("email") or "",
                            conversation_id=conv_id,
                        ),
                    )
                conflitos = checar_conflito(conn, lead["contato_nome"])
                if conflitos:
                    refs = "; ".join(
                        f"{c['processo']} ({c['papel']})" for c in conflitos[:5]
                    )
                    logger.warning("lead=%s POSSÍVEL CONFLITO: %s", lead_id, refs)
                    await notify_mario(
                        jurichat,
                        mario_conversation_id=mario_conversation_id,
                        mensagem=format_notification(
                            tipo="conflito",
                            nome=lead["contato_nome"],
                            telefone=lead["contato_telefone"],
                            ultima_msg=_last_lead_message(transcript),
                            motivo=refs,
                            conversation_id=conv_id,
                        ),
                    )
            except Exception as exc:
                logger.exception(
                    "cliente/conflito check failed for lead=%s: %s",
                    lead_id, exc,
                )

        # Signal 1.6: URGÊNCIA JURÍDICA (roadmap 1.12). Lead com prazo/ato
        # fatal não pode esperar o funil. Escala 🚨 UMA vez e NÃO interrompe
        # (a triagem segue). try/except: falha não trava o ciclo.
        if not lead["urgencia_alertada_em"]:
            try:
                motivo_urgencia = detectar_urgencia(_last_lead_message(transcript))
                if motivo_urgencia:
                    mark_urgencia_alertada(conn, lead_id)
                    logger.info(
                        "lead=%s: urgência detectada (%s) — escalando pro Mario",
                        lead_id, motivo_urgencia,
                    )
                    await notify_mario(
                        jurichat,
                        mario_conversation_id=mario_conversation_id,
                        mensagem=format_notification(
                            tipo="urgencia",
                            nome=lead["contato_nome"],
                            telefone=lead["contato_telefone"],
                            ultima_msg=_last_lead_message(transcript),
                            motivo=motivo_urgencia,
                            conversation_id=conv_id,
                        ),
                    )
            except Exception as exc:
                logger.exception(
                    "urgencia check failed for lead=%s: %s", lead_id, exc,
                )

        # Signal 1.7: ATENDIMENTO "como está meu processo?" (roadmap 2.4).
        # Curto-circuita o funil: o bot responde a consulta de status (ou
        # escala) em vez de tratar como lead. Regras OAB do Mario:
        #   - só passa info pro telefone que está no cadastro como CLIENTE;
        #   - processo em SEGREDO → não responde, escala Mario+Hilde manual;
        #   - telefone não-cadastrado → não revela nada, avisa Mario+Hilde.
        # Em qualquer caso o bot dá baixa pra HUMANO (cliente existente não é
        # lead a nutrir). try/else: sucesso → continue; falha → cai no fluxo
        # normal (degradação graciosa, não trava o ciclo).
        if detectar_pergunta_status(_last_lead_message(transcript)):
            tratou = False
            try:
                procs = consultar_processos_do_telefone(
                    conn, lead["contato_telefone"]
                )
                plano = classificar_atendimento(procs)
                await jurichat.start_human_support(conv_id)

                if plano["acao"] == "responder":
                    # DataJud em paralelo (não serial) pra não segurar o
                    # poll cycle: N processos do MESMO cliente colapsam de
                    # N×8s pra ~8s. Best-effort — falha cai pra data-only.
                    movs_list = await asyncio.gather(
                        *[
                            ultimo_movimento_datajud(
                                p["process_number"], datajud_api_key
                            )
                            for p in plano["publicos"]
                        ],
                        return_exceptions=True,
                    )
                    movimentos: dict = {
                        p["process_number"]: m
                        for p, m in zip(plano["publicos"], movs_list, strict=False)
                        if isinstance(m, dict)
                    }
                    await jurichat.send_message(
                        conv_id, montar_resposta_cliente(plano["publicos"], movimentos)
                    )
                    if plano["sigilosos"]:
                        await notify_mario(
                            jurichat,
                            mario_conversation_id=mario_conversation_id,
                            mensagem=alerta_sigiloso(
                                lead["contato_nome"], lead["contato_telefone"],
                                plano["sigilosos"],
                            ),
                        )
                elif plano["acao"] == "sigiloso":
                    await jurichat.send_message(conv_id, MSG_SIGILOSO_CLIENTE)
                    await notify_mario(
                        jurichat,
                        mario_conversation_id=mario_conversation_id,
                        mensagem=alerta_sigiloso(
                            lead["contato_nome"], lead["contato_telefone"],
                            plano["sigilosos"],
                        ),
                    )
                else:  # nao_cadastrado ou ambiguo — não revela nada, escala
                    await jurichat.send_message(conv_id, MSG_NAO_CADASTRADO_CLIENTE)
                    alerta = (
                        alerta_ambiguo(
                            lead["contato_telefone"], _last_lead_message(transcript),
                        )
                        if plano["acao"] == "ambiguo"
                        else alerta_nao_identificado(
                            lead["contato_telefone"], _last_lead_message(transcript),
                        )
                    )
                    await notify_mario(
                        jurichat,
                        mario_conversation_id=mario_conversation_id,
                        mensagem=alerta,
                    )

                logger.info(
                    "lead=%s: consulta de status — ação=%s (%d processos)",
                    lead_id, plano["acao"], len(procs),
                )
                transicao(
                    conn, lead_id, Estado.AGUARDANDO_HUMANO,
                    motivo=f"atendimento_processo_{plano['acao']}",
                    proxima_acao_horas=CLEAR_PROXIMA_ACAO,
                )
                update_transcript_hash(conn, lead_id, new_hash)
                tratou = True
            except Exception as exc:
                logger.exception(
                    "atendimento_processo (2.4) falhou lead=%s: %s", lead_id, exc,
                )
            if tratou:
                continue

        # Signal 1.8 (bugfix Camila 16/jun): ESCOLHA DE HORÁRIO determinística.
        # Se o bot ofereceu horários e a resposta do lead casa com um deles,
        # CONFIRMA direto — sem depender do Claude, que pode derrapar pro intake
        # e dropar a confirmação até bater o teto de turnos (causa-raiz do bug).
        # Vem ANTES do teto: confirmar é melhor que handoff. Os guardrails de
        # email e data-no-passado seguem no _handle_confirmar_horario.
        #
        # S2 (16/jun): só dispara se o lead AINDA NÃO tem reunião marcada —
        # senão um comentário casual ("aquela terça que você falou") cancelaria
        # e recriaria o evento (remarcação não pedida). Com reunião viva, defere
        # ao Claude (que distingue remarcar de comentário).
        oferecidos = get_horarios_oferecidos(conn, lead_id)
        if oferecidos and not lead["reuniao_event_id"]:
            cal_cfg = calendar or CalendarConfig(
                client=None, business_hours_start=14,
                business_hours_end=19, slot_min=30, buffer_min=0,
                lookahead_days=5, num_slots=4,
            )
            # S5 (16/jun): descarta slots cujo ISO já passou (oferta velha de
            # uma conversa retomada dias depois). Se TODOS venceram, limpa e
            # NÃO casa — segue pro Claude reabrir a agenda (sem loop "já passou").
            oferecidos = [
                s for s in oferecidos
                if _parse_horario_confirmado(s["iso"], cal_cfg.timezone)[0]
                is not None
            ]
            if not oferecidos:
                clear_horarios_oferecidos(conn, lead_id)
            else:
                msg_lead = _last_lead_message(transcript)
                iso_escolhido = casar_horario_escolhido(msg_lead, oferecidos)
                # S3 (16/jun): email-gap. Se há EXATAMENTE 1 slot oferecido, o
                # matcher devolveu None (lead escolheu antes, faltava o email) e
                # agora o lead mandou o email → confirma esse slot único. Só com
                # 1 slot (evita confirmar cego em ambiguidade).
                if (
                    iso_escolhido is None
                    and len(oferecidos) == 1
                    and _extrair_email_do_lead(transcript)
                ):
                    iso_escolhido = oferecidos[0]["iso"]
                if iso_escolhido:
                    logger.info(
                        "lead=%s: horário casado deterministicamente (%s) — "
                        "confirmando sem Claude", lead_id, iso_escolhido,
                    )
                    decisao_det = Decisao(
                        acao="confirmar_horario",
                        mensagem=(
                            "Perfeito! Agendado pra {{HORARIO_CONFIRMADO}}. Te "
                            "enviei o convite no seu email com o link da "
                            "videochamada: {{MEET_LINK}}\n\nAté lá!"
                        ),
                        horario_escolhido_iso=iso_escolhido,
                        lead_email=_extrair_email_do_lead(transcript),
                        resumo_caso="(horário confirmado pela escolha do lead)",
                    )
                    await _handle_confirmar_horario(
                        conn=conn, lead=lead, decisao=decisao_det,
                        transcript=transcript, new_hash=new_hash,
                        jurichat=jurichat,
                        calendar=cal_cfg,
                        mario_conversation_id=mario_conversation_id,
                        poll_interval_seconds=poll_interval_seconds,
                        juridiq=juridiq,
                    )
                    continue

        # Signal 2: turn cap reached → hand off to Mario. Conta a coluna
        # `turnos` (zerada na reativação), NÃO o histórico vitalício de linhas
        # `Lead:` — senão o lead que volta é capado na 1a msg nova (P0 24/jun).
        if lead["turnos"] >= max_turnos:
            clear_horarios_oferecidos(conn, lead_id)  # S6 (16/jun)
            # G4 (2026-06-16): avisa o lead antes do handoff por teto de turnos
            # — antes sumia em silêncio (causa do vácuo no caso Daniel).
            # start_human_support ANTES do send (idempotente): sem isso o
            # Jurichat responde 400 e o lead ficaria mudo no handoff a frio.
            try:
                await jurichat.start_human_support(conv_id)
                await jurichat.send_message(conv_id, _MSG_HANDOFF_LEAD)
            except Exception as exc:
                logger.exception(
                    "send_message(max_turnos lead) failed for lead=%s: %s",
                    lead_id, exc,
                )
            transicao(
                conn, lead_id, Estado.AGUARDANDO_HUMANO,
                motivo="max_turnos",
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            update_transcript_hash(conn, lead_id, new_hash)
            try:
                await notify_mario(
                    jurichat,
                    mario_conversation_id=mario_conversation_id,
                    mensagem=format_notification(
                        tipo="turnos",
                        nome=lead["contato_nome"],
                        telefone=lead["contato_telefone"],
                        ultima_msg=_last_lead_message(transcript),
                        conversation_id=conv_id,
                    ),
                )
            except Exception as exc:
                logger.exception(
                    "notify_mario(turnos) failed for lead=%s: %s", lead_id, exc,
                )
            continue

        # Signal 3: Claude triagem.
        try:
            decisao = await triagem_fn(conversation_transcript=transcript)
        except DecisaoInvalida as exc:
            # Don't update the hash so the next tick retries with the
            # same content. Notify Mario and back off one interval.
            logger.error(
                "triagem returned invalid decision for lead=%s: %s", lead_id, exc,
            )
            register_error(conn, lead_id, "claude_invalid_json")
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
            try:
                await notify_mario(
                    jurichat,
                    mario_conversation_id=mario_conversation_id,
                    mensagem=format_notification(
                        tipo="claude_erro",
                        nome=lead["contato_nome"],
                        telefone=lead["contato_telefone"],
                        ultima_msg=_last_lead_message(transcript),
                        conversation_id=conv_id,
                    ),
                )
            except Exception as exc2:
                logger.exception(
                    "notify_mario(claude_erro) failed for lead=%s: %s",
                    lead_id, exc2,
                )
            continue
        except Exception as exc:
            logger.exception(
                "triagem unexpected error for lead=%s: %s", lead_id, exc,
            )
            register_error(conn, lead_id, "triagem_unexpected_error")
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
            continue

        # P0 (auditoria 24/jun): cada triagem bem-sucedida conta um turno. É o
        # que alimenta o teto de forma RESETÁVEL na reativação (antes contava
        # `Lead:` do transcript vitalício e capava o lead que voltava).
        bump_turnos(conn, lead_id)

        # E3 (auditoria 24/jun; ampliado na revisão adversarial): backstop OAB
        # (Prov. 205/2021) ANTES do dispatch — cobre TODOS os ramos que mandam a
        # prosa do modelo (decisao.mensagem) ao lead, não só o responder:
        # oferecer/confirmar horários e propor também enviam texto autoral (ex.:
        # "Agendado! Garanto o êxito da sua causa"). Se promete resultado/êxito,
        # NÃO vai ao lead: bloqueia, manda msg neutra, passa pro humano e alerta
        # o Mario (decisão dele, 24/jun). Defesa em profundidade — a skill já
        # instrui a não prometer; isto pega o deslize do modelo.
        if contem_promessa_resultado(decisao.mensagem):
            await _bloquear_promessa_resultado(
                conn=conn, lead=lead, decisao=decisao, transcript=transcript,
                new_hash=new_hash, jurichat=jurichat,
                mario_conversation_id=mario_conversation_id,
            )
            continue

        # Dispatch on Claude's decision.
        if decisao.acao == "responder":
            try:
                # Pré-requisito: conversa em human-support mode.
                # Idempotente — chamar repetidamente não tem efeito colateral.
                await jurichat.start_human_support(conv_id)
                await jurichat.send_message(conv_id, decisao.mensagem)
            except Exception as exc:
                logger.exception(
                    "send_message(responder) failed for lead=%s: %s",
                    lead_id, exc,
                )
                register_error(conn, lead_id, "jurichat_send_failed")
                schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
                continue
            update_transcript_hash(conn, lead_id, new_hash)
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)

        elif decisao.acao == "propor":
            # S6 (16/jun): limpa qualquer oferta anterior pendente em todos
            # os ramos. No ramo que oferece horários, o _handle_oferecer_horarios
            # grava slots frescos logo em seguida (clear + set = overwrite).
            clear_horarios_oferecidos(conn, lead_id)
            # Guardrail (bug 2026-06-09): Claude usa "propor" pra leads
            # prontos pra fechar em vez de oferecer agendamento direto.
            # Se temos calendar configurado, REDIRECIONAMOS pra fluxo
            # de agendamento: se já tem email na transcrição, oferece
            # horários direto; senão pede email primeiro.
            # G1 (auditoria 24/jun; revisão adversarial): se o lead RECUSOU
            # videochamada, NÃO redireciona pra Meet — seria insistir no que ele
            # negou; cai no handoff abaixo. O sinal vem do MODELO (campo da
            # Decisao), não de regex: o modelo distingue "recusou vídeo" de "tem
            # restrição de dia/horário" (o regex confundia os dois e mandava lead
            # disposto pra handoff). False = força agendamento (pega misuse de
            # propor pra lead pronto pra fechar).
            if (
                calendar is not None
                and calendar.client is not None
                and not decisao.lead_recusou_videochamada
            ):
                email_na_transcricao = _extrair_email(transcript)
                if email_na_transcricao:
                    # Reusa handler de oferecer_horarios — substitui a
                    # mensagem do Claude por template com placeholder.
                    decisao_oferta = Decisao(
                        acao="oferecer_horarios",
                        mensagem=(
                            "Que ótimo! Pra avançarmos, nossa equipe pode "
                            "te atender por videochamada (Google Meet). "
                            "Tenho esses horários disponíveis:\n\n"
                            "{{HORARIOS}}\n\nQual prefere?"
                        ),
                    )
                    await _handle_oferecer_horarios(
                        conn=conn, lead=lead, decisao=decisao_oferta,
                        transcript=transcript, new_hash=new_hash,
                        jurichat=jurichat, calendar=calendar,
                        mario_conversation_id=mario_conversation_id,
                        poll_interval_seconds=poll_interval_seconds,
                    )
                else:
                    # Falta email — pede primeiro.
                    msg_email = (
                        "Que ótimo! Pra avançarmos, posso agendar uma "
                        "videochamada (Google Meet) com nossa equipe. "
                        "Qual seu melhor email pra eu te enviar o convite?"
                    )
                    try:
                        await jurichat.start_human_support(conv_id)
                        await jurichat.send_message(conv_id, msg_email)
                    except Exception as exc:
                        logger.exception(
                            "send_message(propor_pede_email) lead=%s: %s",
                            lead_id, exc,
                        )
                        register_error(conn, lead_id, "jurichat_send_failed")
                    update_transcript_hash(conn, lead_id, new_hash)
                    schedule_next_action_seconds(
                        conn, lead_id, poll_interval_seconds,
                    )
                continue

            # Sem calendar configurado — fallback antigo (handoff humano).
            try:
                await jurichat.start_human_support(conv_id)
                await jurichat.send_message(conv_id, decisao.mensagem)
            except Exception as exc:
                logger.exception(
                    "send_message(propor) failed for lead=%s: %s",
                    lead_id, exc,
                )
                register_error(conn, lead_id, "jurichat_send_failed")
                schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
                continue
            transicao(
                conn, lead_id, Estado.AGUARDANDO_HUMANO,
                motivo="claude_propor",
                payload={"resumo_caso": decisao.resumo_caso},
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            update_transcript_hash(conn, lead_id, new_hash)
            try:
                await notify_mario(
                    jurichat,
                    mario_conversation_id=mario_conversation_id,
                    mensagem=format_notification(
                        tipo="fechar",
                        nome=lead["contato_nome"],
                        telefone=lead["contato_telefone"],
                        ultima_msg=_last_lead_message(transcript),
                        resumo=decisao.resumo_caso,
                        conversation_id=conv_id,
                    ),
                )
            except Exception as exc:
                logger.exception(
                    "notify_mario(fechar) failed for lead=%s: %s", lead_id, exc,
                )

        elif decisao.acao == "oferecer_horarios":
            await _handle_oferecer_horarios(
                conn=conn, lead=lead, decisao=decisao,
                transcript=transcript, new_hash=new_hash,
                jurichat=jurichat,
                calendar=calendar or CalendarConfig(
                    client=None, business_hours_start=14,
                    business_hours_end=19, slot_min=30, buffer_min=0,
                    lookahead_days=5, num_slots=4,
                ),
                mario_conversation_id=mario_conversation_id,
                poll_interval_seconds=poll_interval_seconds,
            )

        elif decisao.acao == "confirmar_horario":
            await _handle_confirmar_horario(
                conn=conn, lead=lead, decisao=decisao,
                transcript=transcript, new_hash=new_hash,
                jurichat=jurichat,
                calendar=calendar or CalendarConfig(
                    client=None, business_hours_start=14,
                    business_hours_end=19, slot_min=30, buffer_min=0,
                    lookahead_days=5, num_slots=4,
                ),
                mario_conversation_id=mario_conversation_id,
                poll_interval_seconds=poll_interval_seconds,
                juridiq=juridiq,
            )

        elif decisao.acao == "remarcar_reuniao":
            await _handle_remarcar_reuniao(
                conn=conn, lead=lead, decisao=decisao,
                transcript=transcript, new_hash=new_hash,
                jurichat=jurichat,
                calendar=calendar or CalendarConfig(
                    client=None, business_hours_start=14,
                    business_hours_end=19, slot_min=30, buffer_min=0,
                    lookahead_days=5, num_slots=4,
                ),
                mario_conversation_id=mario_conversation_id,
                poll_interval_seconds=poll_interval_seconds,
            )

        elif decisao.acao == "cancelar_reuniao":
            await _handle_cancelar_reuniao(
                conn=conn, lead=lead, decisao=decisao,
                new_hash=new_hash,
                jurichat=jurichat,
                calendar=calendar or CalendarConfig(
                    client=None, business_hours_start=14,
                    business_hours_end=19, slot_min=30, buffer_min=0,
                    lookahead_days=5, num_slots=4,
                ),
                mario_conversation_id=mario_conversation_id,
                poll_interval_seconds=poll_interval_seconds,
            )

        elif decisao.acao == "handoff":
            clear_horarios_oferecidos(conn, lead_id)  # S6 (16/jun)
            # G4 (2026-06-16): handoff NÃO é mais mudo — avisa o lead antes de
            # passar pro humano. Usa a mensagem do Claude se houver; senão o
            # padrão. (Causa-raiz do silêncio de ~1h no caso Daniel.)
            msg_lead = (decisao.mensagem or "").strip() or _MSG_HANDOFF_LEAD
            # Defesa: handoff é o único send que não passa por replace — tira
            # placeholder cru ({{HORARIOS}} etc.) que o Claude possa ter deixado.
            msg_lead = _RE_PLACEHOLDER.sub("", msg_lead).strip() or _MSG_HANDOFF_LEAD
            try:
                # start_human_support ANTES do send (idempotente): sem isso o
                # Jurichat responde 400 e o lead ficaria mudo no handoff a frio.
                await jurichat.start_human_support(conv_id)
                await jurichat.send_message(conv_id, msg_lead)
            except Exception as exc:
                logger.exception(
                    "send_message(handoff lead) failed for lead=%s: %s",
                    lead_id, exc,
                )
            transicao(
                conn, lead_id, Estado.AGUARDANDO_HUMANO,
                motivo="claude_handoff",
                payload={"motivo_handoff": decisao.motivo_handoff},
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            update_transcript_hash(conn, lead_id, new_hash)
            try:
                await notify_mario(
                    jurichat,
                    mario_conversation_id=mario_conversation_id,
                    mensagem=format_notification(
                        tipo="handoff",
                        nome=lead["contato_nome"],
                        telefone=lead["contato_telefone"],
                        ultima_msg=_last_lead_message(transcript),
                        motivo=decisao.motivo_handoff,
                        resumo=decisao.resumo_caso,
                        conversation_id=conv_id,
                    ),
                )
            except Exception as exc:
                logger.exception(
                    "notify_mario(handoff) failed for lead=%s: %s",
                    lead_id, exc,
                )

        else:  # pragma: no cover — parse_decisao guards this
            logger.error(
                "unknown acao %r for lead=%s — skipping", decisao.acao, lead_id,
            )
            register_error(conn, lead_id, "unknown_acao")
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)

    # F1 (auditoria 24/jun): fechado o loop, varre os leads presos em falha
    # recorrente e alerta o Mario UMA vez por lead (visibilidade — antes ficavam
    # mudos e invisíveis). Fora do loop pra não duplicar alerta no mesmo tick.
    await _alertar_leads_presos(conn, jurichat, mario_conversation_id)


_RE_CANCELAMENTO = re.compile(r"\b(?:cancel|desmarc)\w*", re.IGNORECASE)


async def _lead_pediu_cancelamento(
    jurichat: JurichatClient, conv_id: str,
) -> str | None:
    """Devolve a ÚLTIMA fala do LEAD se ela pede cancelamento; senão None.

    Roda DENTRO do ciclo de lembretes — rede de segurança pro lead que o brain
    NÃO reprocessa (modo-humano / agendado manual), origem do bug Daniel
    (19/jun): o lembrete saía mesmo depois do "cancela". Conservador: só a
    última linha ``Lead:`` conta, e só dispara em palavra clara (cancel*/
    desmarc*). Em erro de rede devolve None → degrada pra mandar o lembrete (não
    "engole" a reunião por uma falha transitória).
    """
    try:
        conv = await jurichat.get_conversation(conv_id)
        transcript = (
            (conv.get("transcription") or "") if isinstance(conv, dict) else ""
        )
        for line in reversed(transcript.splitlines()):
            s = line.strip()
            if s.startswith("Lead:"):
                fala = s[len("Lead:"):].strip()
                return fala if _RE_CANCELAMENTO.search(fala) else None
    except Exception as exc:  # noqa: BLE001 — nunca derruba o ciclo de lembretes
        logger.warning("cancelamento-check conv=%s: %s — segue", conv_id, exc)
    return None


async def _cancelar_reuniao_auto(
    *,
    conn: Any,
    lead: dict[str, Any],
    jurichat: JurichatClient,
    calendar: "CalendarConfig | None",
    mario_conversation_id: str,
    horario_human: str,
    ultima_msg: str,
) -> None:
    """Cancelamento detectado FORA do brain, no ciclo de lembretes.

    Cancela o evento (se houver), limpa a reunião (zera reuniao_em + flags → os
    lembretes param) e AVISA O MARIO. NÃO responde ao lead — a conversa está com
    atendimento humano; quem responde é o Mario.
    """
    lead_id = lead["id"]
    event_id = lead["reuniao_event_id"]
    if (
        calendar is not None
        and getattr(calendar, "client", None) is not None
        and event_id
    ):
        try:
            await calendar.client.cancel_event(event_id)
        except Exception as exc:  # noqa: BLE001 — falha no Calendar não trava o resto
            logger.warning(
                "cancel_event(auto-lembrete) lead=%s event=%s: %s — segue",
                lead_id, event_id, exc,
            )
    clear_reuniao(conn, lead_id)
    logger.info(
        "auto-cancelamento via lembrete: lead=%s pediu cancelamento — reuniao limpa",
        lead_id,
    )
    if mario_conversation_id:
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=(
                "❌ *Reunião auto-cancelada*\n\n"
                f"Lead: {lead['contato_nome']}\n"
                f"Tel: {lead['contato_telefone']}\n"
                f"Era: {horario_human}\n\n"
                f"O lead pediu cancelamento (\"{ultima_msg[:140]}\") e a conversa "
                "está com atendimento humano. Parei os lembretes e limpei a "
                "reunião — responda o lead você. Se foi engano, é só reagendar."
            ),
        )


_NOSHOW_GRACE = datetime.timedelta(hours=1)


async def _ping_noshow(
    conn: Any,
    lead: dict[str, Any],
    jurichat: JurichatClient,
    mario_conversation_id: str,
    base_url: str,
) -> None:
    """Ping de no-show ao Mario, 5 min após o início, com link de 1 toque pra
    cancelar + remarcar. Grava o token (= avisado → dispara UMA vez). O bot NÃO
    cancela sozinho — quem decide é o Mario (ele está na reunião e sabe se a
    pessoa apareceu). Semi-auto pedido por ele em 20/jun.
    """
    token = secrets.token_urlsafe(24)
    if not mario_conversation_id:
        # Ninguém pra avisar — grava o token mesmo assim pra não re-entrar
        # neste ramo todo tick (não há alerta a perder).
        marcar_noshow_avisado(conn, lead["id"], token)
        return
    horario = ""
    if lead["reuniao_em"]:
        try:
            horario = _format_reuniao_human(
                datetime.datetime.fromisoformat(
                    lead["reuniao_em"]
                ).astimezone(datetime.UTC)
            )
        except (ValueError, TypeError):
            horario = lead["reuniao_em"]
    link = (
        f"{base_url.rstrip('/')}/reuniao/cancelar/{token}"
        if base_url else "(FUNIL_BASE_URL não configurada)"
    )
    # D3 (auditoria 24/jun): só grava o token (= avisado) se o alerta de fato
    # saiu. Antes marcava ANTES de enviar — uma falha no notify_mario (Jurichat
    # fora) sumia com o aviso de no-show pra sempre (condição de re-ping exige
    # noshow_token IS NULL). Agora espelha _enviar_lembrete: falha → re-tenta no
    # próximo tick enquanto a janela de no-show durar.
    enviado = await notify_mario(
        jurichat,
        mario_conversation_id=mario_conversation_id,
        mensagem=(
            "⏰ *Possível no-show*\n\n"
            f"Lead: {lead['contato_nome']}\n"
            f"Tel: {lead['contato_telefone']}\n"
            f"Reunião: {horario}\n\n"
            "A videochamada começou há 5 min. O lead compareceu?\n\n"
            "✅ Se SIM, é só ignorar esta mensagem.\n"
            "❌ Se foi no-show, cancele + ofereça remarcação em 1 toque:\n"
            f"{link}"
        ),
    )
    if enviado:
        marcar_noshow_avisado(conn, lead["id"], token)


def _convidados_externos(ev: dict[str, Any]) -> list[str]:
    """D4 (25/jun): emails dos attendees que NÃO são o dono/organizador do
    calendário (flags self/organizer do Google) — i.e., o cliente convidado."""
    return [
        a["email"]
        for a in ev.get("attendees", [])
        if a.get("email") and not a.get("self") and not a.get("organizer")
    ]


async def _alertar_evento_manual(
    conn: Any,
    jurichat: JurichatClient,
    mario_conversation_id: str,
    ev: dict[str, Any],
    *,
    motivo: str,
) -> None:
    """D4 (25/jun): alerta o Mario UMA vez sobre um evento manual não-rastreado/
    conflito, com o comando preenchido pra registrar (--conversa em branco, ele
    completa). Dedup via eventos_manuais_alertados."""
    event_id = ev["id"]
    if evento_manual_ja_alertado(conn, event_id):
        return
    convidados = ", ".join(_convidados_externos(ev)) or "?"
    cmd = (
        ".venv/bin/python scripts/registrar_reuniao_manual.py "
        f"--conversa <ID_DA_CONVERSA> --quando {ev['start_iso']} "
        f"--meet {ev['meet_link'] or '-'} --event-id {event_id}"
    )
    enviado = await notify_mario(
        jurichat,
        mario_conversation_id=mario_conversation_id,
        mensagem=(
            "📅 Reunião no Calendar NÃO está nos lembretes automáticos:\n\n"
            f"{ev['summary'] or '(sem título)'}\n"
            f"Quando: {ev['start_iso']}\n"
            f"Convidado(s): {convidados}\n"
            f"Motivo: {motivo}\n\n"
            "Se é cliente e quer os lembretes, registre (preencha a conversa):\n"
            f"{cmd}"
        ),
    )
    if enviado or not mario_conversation_id:
        marcar_evento_manual_alertado(conn, event_id)


async def sync_reunioes_manuais(
    *,
    get_db: Callable[[], Any],
    calendar: "CalendarConfig | None",
    jurichat: JurichatClient,
    mario_conversation_id: str = "",
    janela_horas: int = 48,
) -> None:
    """D4 (25/jun): detecta reuniões marcadas FORA do bot no Google Calendar:
    - auto-vincula ao lead pelo email do convidado → lembretes 24h/2h/30min saem;
    - alerta o Mario 1× sobre eventos com convidado externo que não casam (ou
      conflitam com outra reunião do lead).
    Ignora eventos já rastreados (event_id num lead), all-day e sem convidado
    externo (audiência/pessoal). Best-effort: nunca derruba o ciclo."""
    if calendar is None or calendar.client is None:
        return
    conn = get_db()
    now = datetime.datetime.now(datetime.UTC)
    try:
        eventos = await calendar.client.list_events(
            time_min=now,
            time_max=now + datetime.timedelta(hours=janela_horas),
        )
    except Exception as exc:
        logger.warning("sync_reunioes_manuais: list_events falhou: %s", exc)
        return

    tracked = event_ids_de_reunioes(conn)
    for ev in eventos:
        event_id = ev["id"]
        if not event_id or event_id in tracked:
            continue  # já rastreado pelo bot (ou sem id)
        if not ev["start_iso"]:
            continue  # all-day (audiência sem horário) → ignora
        externos = _convidados_externos(ev)
        if not externos:
            continue  # sem convidado externo → audiência/pessoal, ignora

        casados = {
            row["id"]: row
            for email in externos
            for row in lead_por_email(conn, email)
        }

        if len(casados) != 1:
            # 0 = não rastreado; >1 = ambíguo. Alerta pro Mario decidir.
            motivo = (
                "não casei com nenhum lead pelo email"
                if not casados
                else f"AMBÍGUO — {len(casados)} leads com esse email"
            )
            await _alertar_evento_manual(
                conn, jurichat, mario_conversation_id, ev, motivo=motivo,
            )
            continue

        lead = next(iter(casados.values()))
        # Lead já tem OUTRA reunião marcada? NÃO sobrescreve (1 reunião/lead).
        if lead["reuniao_em"] and (lead["reuniao_event_id"] or "") != event_id:
            await _alertar_evento_manual(
                conn, jurichat, mario_conversation_id, ev,
                motivo=(
                    f"CONFLITO — {lead['contato_nome']} já tem reunião em "
                    f"{lead['reuniao_em']}; este evento manual é outro"
                ),
            )
            continue

        # Auto-vincula → o reminder_cycle cuida dos lembretes.
        try:
            set_reuniao(
                conn, lead["id"],
                reuniao_em_iso=ev["start_iso"], event_id=event_id,
                meet_link=ev["meet_link"],
            )
        except ValueError as exc:
            logger.warning(
                "sync manual: set_reuniao ISO inválido %r: %s",
                ev["start_iso"], exc,
            )
            continue
        logger.info(
            "D4: reunião manual %s vinculada ao lead=%s", event_id, lead["id"],
        )
        try:
            await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=(
                    "✅ Vinculei uma reunião marcada por fora aos lembretes:\n\n"
                    f"Lead: {lead['contato_nome']}\n"
                    f"Quando: {ev['start_iso']}\n"
                    f"({ev['summary'] or 'sem título'})\n\n"
                    "Os lembretes 24h/2h/30min saem automaticamente."
                ),
            )
        except Exception as exc:
            logger.exception(
                "notify_mario(reuniao_manual_vinculada) falhou: %s", exc,
            )


# Sweeper do pós-assinatura (#36, 25/jun) — retoma sub-passos que falharam (o
# webhook signed não reentrega após o 200). Trabalho LIMITADO por tick.
_POS_SWEEP_LIMIT = 10
_POS_MAX_TENTATIVAS = 5


def _pos_pendente(row: Any) -> bool:
    """True se algum sub-passo ainda falta (mesma regra da query do sweep)."""
    return (
        row["intake_juridiq_em"] is None
        or row["arquivo_pdf_em"] is None
        or (row["tarefa_abertura_em"] is None and row["person_id"] is not None)
    )


async def _escalar_pos_se_travado(
    conn: Any,
    contrato_id: int,
    cliente_nome: str,
    jurichat: JurichatClient,
    mario_conversation_id: str,
) -> None:
    """Após o teto de tentativas, se ainda há passo pendente: TRAVA o contrato
    (sai da fila do sweep) e alerta o Mario 1× pra resolver à mão. Re-lê o estado
    do banco (não confia em contador otimista)."""
    row = conn.execute(
        "SELECT pos_tentativas, intake_juridiq_em, arquivo_pdf_em, "
        "tarefa_abertura_em, person_id FROM contrato WHERE id = ?",
        (contrato_id,),
    ).fetchone()
    if row is None or row["pos_tentativas"] < _POS_MAX_TENTATIVAS:
        return
    if not _pos_pendente(row):
        return  # resolveu tudo na última tentativa
    marcar_pos_travado(conn, contrato_id)
    if not mario_conversation_id:
        return
    faltam = []
    if row["intake_juridiq_em"] is None:
        faltam.append("ficha")
    if row["arquivo_pdf_em"] is None:
        faltam.append("PDF")
    if row["tarefa_abertura_em"] is None and row["person_id"] is not None:
        faltam.append("tarefa")
    try:
        await notify_mario(
            jurichat,
            mario_conversation_id=mario_conversation_id,
            mensagem=(
                f"⚠️ Pós-assinatura TRAVADO — {cliente_nome} (contrato "
                f"#{contrato_id}) após {_POS_MAX_TENTATIVAS} tentativas. "
                f"Falta: {', '.join(faltam)}. Resolver à mão no Juridiq."
            ),
        )
    except Exception as exc:
        logger.warning("notify_mario(pos_travado) falhou: %s", exc)


async def sweep_pos_assinatura(
    *,
    get_db: Callable[[], Any],
    zapsign: ZapSignClient | None,
    juridiq: JuridiqClient | None,
    jurichat: JurichatClient,
    settings: Any,
) -> None:
    """Retoma contratos ASSINADOS com sub-passo do pós PENDENTE (intake/arquivo/
    tarefa). Re-busca o doc na ZapSign (URL signed_file FRESCA — a salva expira em
    ~60min) e re-roda processar_pos_assinatura (idempotente por-passo). Teto de
    tentativas por contrato → trava + alerta. Best-effort: NUNCA derruba o ciclo."""
    if zapsign is None or juridiq is None:
        return
    conn = get_db()
    try:
        pendentes = list_contratos_pos_pendentes(conn, limite=_POS_SWEEP_LIMIT)
    except Exception as exc:
        logger.warning("sweep_pos: list falhou: %s", exc)
        return
    for c in pendentes:
        cid = c["id"]
        try:
            registrar_tentativa_pos(conn, cid)
            doc = await zapsign.get_doc(c["zapsign_doc_token"])
            await processar_pos_assinatura(
                conn, juridiq=juridiq, zapsign=zapsign, jurichat=jurichat,
                settings=settings, contrato_id=cid,
                signed_file_url=doc.get("signed_file"),
            )
            await _escalar_pos_se_travado(
                conn, cid, c["cliente_nome"], jurichat,
                settings.mario_conversation_id,
            )
        except Exception:
            logger.exception("sweep_pos: contrato=%s", cid)


async def run_reminder_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    mario_conversation_id: str = "",
    calendar: "CalendarConfig | None" = None,
    base_url: str = "",
) -> None:
    """Manda lembretes 24h / 2h / 30min de cada reunião agendada.

    Lógica por lead com ``reuniao_em`` futuro:
      - Se já passou (delta < 0): clear_reuniao (limpa flag).
      - Se delta ≤ 24h e !24h_enviado: manda 24h, marca.
      - Se delta ≤ 2h e !2h_enviado: manda 2h, marca.
      - Se delta ≤ 30min e !30min_enviado: manda 30min, marca.

    Múltiplos lembretes podem disparar no mesmo tick (ex: reunião marcada
    pra daqui 15 min → manda 30min imediato; reunião pra daqui 1h →
    poderia mandar 2h e 30min em sequência se nenhum foi marcado, mas o
    set_reuniao já pré-marca os "perdidos" como enviados).
    """
    conn = get_db()
    leads = list_leads_com_reuniao_futura(conn)
    logger.info("reminder tick: %d leads com reuniao futura", len(leads))

    now = datetime.datetime.now(datetime.UTC)
    for lead in leads:
        try:
            reuniao_dt = datetime.datetime.fromisoformat(
                lead["reuniao_em"]
            ).astimezone(datetime.UTC)
        except (ValueError, TypeError) as exc:
            # D5 (auditoria 24/jun): reuniao_em inparseável = reunião fantasma.
            # Antes só logava + continue → ficava presa AQUI todo tick (nunca
            # lembrava, nunca virava no-show, nunca era limpa). Agora limpa +
            # alerta o Mario pra ele remarcar manual se era real.
            logger.error(
                "lead=%s reuniao_em inválido %r — alertando + limpando: %s",
                lead["id"], lead["reuniao_em"], exc,
            )
            # Revisão adversarial 24/jun: alerta ANTES de limpar e só limpa se o
            # aviso saiu (mesmo padrão do D3). A conexão é autocommit, então um
            # clear_reuniao antes do notify, com o Jurichat fora, zerava o
            # reuniao_em na hora → o lead sumia de list_leads_com_reuniao_futura
            # e o alerta de uma reunião REAL (fantasma legado) se perdia pra
            # sempre. Falha de envio agora re-tenta no próximo tick. Sem Mario
            # configurado não há o que avisar → limpa pra não travar o loop.
            enviado = await notify_mario(
                jurichat,
                mario_conversation_id=mario_conversation_id,
                mensagem=(
                    "⚠️ Uma reunião com horário inválido no sistema foi "
                    f"detectada (lead: {lead['contato_nome']}, "
                    f"tel: {lead['contato_telefone']}). "
                    "Vou removê-la dos lembretes; se era real, remarque manual."
                ),
            )
            if enviado or not mario_conversation_id:
                clear_reuniao(conn, lead["id"])
            continue

        delta = reuniao_dt - now

        # Reunião já começou/passou.
        if delta.total_seconds() < 0:
            passou = -delta
            if passou >= _NOSHOW_GRACE:
                # Bem passada (além da janela de no-show) → limpa.
                logger.info("lead=%s reuniao passou, limpando", lead["id"])
                clear_reuniao(conn, lead["id"])
            elif (
                passou >= datetime.timedelta(minutes=5)
                and lead["noshow_token"] is None
            ):
                # 5+ min após o início, ainda sem avisar → ping de no-show ao
                # Mario com link de 1 toque (semi-auto: ele decide se cancela).
                await _ping_noshow(
                    conn, lead, jurichat, mario_conversation_id, base_url,
                )
            # else: <5min (em andamento) ou já avisado → espera.
            continue

        meet_link = lead["reuniao_meet_link"] or ""
        nome = lead["contato_nome"] or "Olá"
        conv_id = lead["jurichat_conversation_id"]
        horario_human = _format_reuniao_human(reuniao_dt)

        # Decide quais lembretes disparar neste tick. Ordem importa:
        # se faltam 5 min e nenhum foi enviado, manda APENAS o 30min
        # (não faria sentido mandar "24h" agora).
        # Qual lembrete está pendente neste tick (ordem: 30min > 2h > 24h).
        tag: str | None = None
        if delta <= datetime.timedelta(minutes=5):
            tag = "5min" if lead["lembrete_5min_enviado_em"] is None else None
        elif delta <= datetime.timedelta(minutes=30):
            tag = "30min" if lead["lembrete_30min_enviado_em"] is None else None
        elif delta <= datetime.timedelta(hours=2):
            tag = "2h" if lead["lembrete_2h_enviado_em"] is None else None
        elif delta <= datetime.timedelta(hours=24):
            tag = "24h" if lead["lembrete_24h_enviado_em"] is None else None
        # Rede de segurança pro lead que o brain NÃO reprocessa (modo-humano /
        # agendado manual) — origem do bug Daniel (19/jun): o lembrete saía
        # depois do "cancela". Se pediu, cancela a reunião + avisa o Mario.
        # (Lead em em_conversa: o brain já limpou reuniao_em antes → nem entra
        # na lista deste ciclo.)
        # G4 (auditoria 24/jun): roda ANTES do `if tag is None`, dentro da janela
        # de lembretes (<=24h). Antes vinha DEPOIS do tag-gate, então um "cancela"
        # após o 5min (todos os lembretes enviados → tag None) era IGNORADO: o
        # no-show ping disparava e o Mario esperava na chamada. Escopado a <=24h
        # pra não consultar a conversa de reuniões distantes a cada tick.
        if delta <= datetime.timedelta(hours=24):
            ultima = await _lead_pediu_cancelamento(jurichat, conv_id)
            if ultima is not None:
                await _cancelar_reuniao_auto(
                    conn=conn, lead=lead, jurichat=jurichat, calendar=calendar,
                    mario_conversation_id=mario_conversation_id,
                    horario_human=horario_human, ultima_msg=ultima,
                )
                continue

        if tag is None:
            continue

        msg = {
            "5min": _msg_lembrete_5min,
            "30min": _msg_lembrete_30min,
            "2h": _msg_lembrete_2h,
            "24h": _msg_lembrete_24h,
        }[tag](nome, horario_human, meet_link)
        if await _enviar_lembrete(jurichat, conv_id, msg, lead["id"], tag):
            mark_lembrete_enviado(conn, lead["id"], tag)
            # Signal 3.2: briefing pré-reunião pra EQUIPE (interno), junto do
            # lembrete de 2h. Se cliente, lista os processos (do
            # cliente_processo, instantâneo). try/except: nunca derruba o ciclo.
            if tag == "2h" and mario_conversation_id:
                try:
                    procs = consultar_processos_do_telefone(
                        conn, lead["contato_telefone"]
                    )
                    await notify_mario(
                        jurichat,
                        mario_conversation_id=mario_conversation_id,
                        mensagem=montar_briefing(
                            lead["contato_nome"], lead["contato_telefone"],
                            horario_human, meet_link, procs,
                        ),
                    )
                except Exception as exc:
                    logger.exception(
                        "briefing 3.2 falhou lead=%s: %s", lead["id"], exc,
                    )


def _format_reuniao_human(dt: datetime.datetime) -> str:
    """Format reunião pro lembrete — usa o tz do Mario."""
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("America/Sao_Paulo")
    local = dt.astimezone(tz)
    dias = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]
    meses = [
        "jan", "fev", "mar", "abr", "mai", "jun",
        "jul", "ago", "set", "out", "nov", "dez",
    ]
    hora = local.strftime("%Hh") if local.minute == 0 else local.strftime("%Hh%M")
    return f"{dias[local.weekday()]} ({local.day:02d}/{meses[local.month-1]}) às {hora}"


def _msg_lembrete_24h(nome: str, horario: str, meet_link: str) -> str:
    base = (
        f"Oi {nome}! Lembrete: amanhã ({horario}) temos a videochamada "
        f"com nossa equipe."
    )
    if meet_link:
        base += f"\n\nLink Meet: {meet_link}"
    base += (
        "\n\nSe precisar remarcar ou tiver alguma dúvida, é só me chamar "
        "por aqui."
    )
    return base


def _msg_lembrete_2h(nome: str, horario: str, meet_link: str) -> str:
    base = f"Oi {nome}! Em 2 horas começa nossa videochamada ({horario})."
    if meet_link:
        base += f"\n\nLink Meet: {meet_link}"
    base += "\n\nTe vejo lá!"
    return base


def _msg_lembrete_30min(nome: str, horario: str, meet_link: str) -> str:
    base = (
        f"{nome}, lembrete rápido: em 30 minutos começa a videochamada "
        f"com nossa equipe."
    )
    if meet_link:
        base += f"\n\nLink Meet: {meet_link}"
    return base


def _msg_lembrete_5min(nome: str, horario: str, meet_link: str) -> str:
    base = (
        f"{nome}, sua videochamada com nossa equipe começa em 5 minutos! 🎥"
    )
    if meet_link:
        base += f"\n\nLink Meet: {meet_link}"
    # G3 (auditoria 24/jun): NÃO promete "cancelamento automático" — o código
    # nunca cancela sozinho (no-show é semi-auto, o Mario decide). Texto antigo
    # fazia o lead atrasado confiar que tinha sido cancelado e não entrar,
    # deixando o Mario esperando na chamada. Alinhado ao comportamento real.
    base += (
        "\n\nSe atrasar, sem problema — é só me avisar por aqui que a gente "
        "remarca pra outro horário. Te esperamos!"
    )
    return base


async def _enviar_lembrete(
    jurichat: JurichatClient, conv_id: str, msg: str,
    lead_id: int, tag: str,
) -> bool:
    """Envia lembrete. Retorna True só se o envio CONFIRMOU.

    Auditoria 2026-06-11: a flag era marcada mesmo com envio falho —
    lembrete perdido pra sempre. Agora o caller só marca em sucesso;
    falha → re-tenta no próximo tick (enquanto a janela durar).
    """
    try:
        await jurichat.start_human_support(conv_id)
        await jurichat.send_message(conv_id, msg)
        logger.info("lembrete %s enviado pra lead=%s", tag, lead_id)
        return True
    except Exception as exc:
        logger.exception(
            "lembrete %s falhou pra lead=%s: %s — re-tenta no próximo tick",
            tag, lead_id, exc,
        )
        return False


def _humano_assumiu_conv(conv: dict[str, Any], bot_user_id: str) -> bool:
    """True se o responsável ATUAL da conversa não é o bot (humano assumiu pelo
    painel) — mesmo predicado do Signal 0 do poll cycle. False se bot_user_id
    vazio (feature desligada) ou sem ``user`` identificável."""
    if not bot_user_id:
        return False
    user = conv.get("user") or {}
    user_id = user.get("id") if isinstance(user, dict) else None
    return bool(user_id and user_id != bot_user_id)


async def run_followup_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    gerar_followup_msg: Callable[..., Awaitable[str]],
    followup_2_apos_horas: int,
    encerramento_apos_horas: int,
    followup_1_apos_horas: int = 48,
    bot_user_id: str = "",
) -> None:
    """Process all due leads in a single pass."""
    conn = get_db()
    vencidos = list_leads_vencidos(conn, fu1_apos_horas=followup_1_apos_horas)
    logger.info("scheduler tick: %d leads vencidos", len(vencidos))

    for lead in vencidos:
        # E1 (auditoria 24/jun): NÃO manda follow-up pra quem está na lista de
        # supressão (opt-out LGPD). O poll cycle registra o opt-out, mas este
        # ciclo — o sender de MAIOR volume — nunca consultava esta_suprimido,
        # furando a própria garantia LGPD do módulo. Pula ANTES de qualquer
        # chamada ao Jurichat e encerra em AGUARDANDO_HUMANO (motivo opt_out,
        # terminal pra reativação — não volta a contatar sozinho).
        if esta_suprimido(conn, telefone=lead["contato_telefone"] or ""):
            logger.info(
                "followup: lead=%s na supressão (opt-out) — pula sem enviar",
                lead["id"],
            )
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO, motivo="opt_out",
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            continue

        try:
            tags = await jurichat.get_lead_tags(lead["jurichat_lead_id"])
        except Exception as exc:
            logger.exception("get_lead_tags failed for %s: %s", lead["id"], exc)
            register_error(conn, lead["id"], "jurichat_get_tags_failed")
            continue

        if not is_eligible_for_followup(tags):
            # Tag de exclusão (Cliente Ativo etc.) = humano cuida.
            # Transiciona pra AGUARDANDO_HUMANO — antes só limpava
            # proxima_acao_em, o que com o critério novo (ultima_msg)
            # faria o lead ser re-avaliado a cada tick pra sempre.
            register_error(conn, lead["id"], "excluido_followup_etiqueta")
            transicao(
                conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                motivo="excluido_followup_etiqueta",
                payload={"tags": list(tags)},
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            continue

        estado = lead["estado"]
        try:
            if estado == Estado.EM_CONVERSA:
                conv = await jurichat.get_conversation(
                    lead["jurichat_conversation_id"]
                )
                # C2 (auditoria 24/jun): não dispara follow-up por cima de um
                # humano que assumiu pelo painel (Signal 0). Pausa pra
                # AGUARDANDO_HUMANO em vez de mandar FU1.
                if _humano_assumiu_conv(conv, bot_user_id):
                    logger.info(
                        "followup: humano assumiu lead=%s — pausa (sem FU1)",
                        lead["id"],
                    )
                    transicao(
                        conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                        motivo="humano_assumiu_conversa",
                        proxima_acao_horas=CLEAR_PROXIMA_ACAO,
                    )
                    continue
                texto = await gerar_followup_msg(
                    conversation_transcript=conv.get("transcription", ""),
                )
                # Atomic: transition state AND advance schedule together.
                # If we transitioned without rescheduling, a crash would
                # leave the lead picked up again next tick and the
                # WhatsApp message would fire twice (non-idempotent).
                transicao(
                    conn, lead["id"], Estado.FOLLOW_UP_1_ENVIADO,
                    motivo="scheduler_followup_1",
                    proxima_acao_horas=followup_2_apos_horas,
                )
                # Send AFTER state+schedule are committed. If send fails
                # the lead is correctly scheduled for the NEXT cycle and
                # we won't double-send on retry (state is already FU1).
                await jurichat.start_human_support(
                    lead["jurichat_conversation_id"],
                )
                await jurichat.send_message(
                    lead["jurichat_conversation_id"], texto,
                )

            elif estado == Estado.FOLLOW_UP_1_ENVIADO:
                # C2: idem — checa Signal 0 antes do FU2 (humano pode ter
                # assumido depois do FU1).
                conv = await jurichat.get_conversation(
                    lead["jurichat_conversation_id"]
                )
                if _humano_assumiu_conv(conv, bot_user_id):
                    logger.info(
                        "followup: humano assumiu lead=%s — pausa (sem FU2)",
                        lead["id"],
                    )
                    transicao(
                        conn, lead["id"], Estado.AGUARDANDO_HUMANO,
                        motivo="humano_assumiu_conversa",
                        proxima_acao_horas=CLEAR_PROXIMA_ACAO,
                    )
                    continue
                nome = lead["contato_nome"] or "Olá"
                texto = FOLLOWUP_2_TEXT.format(nome=nome)
                transicao(
                    conn, lead["id"], Estado.FOLLOW_UP_2_ENVIADO,
                    motivo="scheduler_followup_2",
                    proxima_acao_horas=encerramento_apos_horas,
                )
                await jurichat.start_human_support(
                    lead["jurichat_conversation_id"],
                )
                await jurichat.send_message(
                    lead["jurichat_conversation_id"], texto,
                )

            elif estado == Estado.FOLLOW_UP_2_ENVIADO:
                # Silent close — no new message. Atomic transition + clear.
                transicao(
                    conn, lead["id"], Estado.ENCERRADO_SEM_RESPOSTA,
                    motivo="scheduler_encerramento",
                    proxima_acao_horas=CLEAR_PROXIMA_ACAO,
                )

            else:
                logger.warning(
                    "lead %s in unexpected scheduler state %s", lead["id"], estado,
                )
        except Exception as exc:
            logger.exception(
                "scheduler step failed for lead %s: %s", lead["id"], exc,
            )
            register_error(conn, lead["id"], "scheduler_step_failed")


def main() -> int:
    """Entry point for `noviello-followup` console script.

    Reads settings, opens DB + client, runs BOTH cycles (poll first, then
    follow-up), exits 0/1.
    """
    from functools import partial

    from anthropic import AsyncAnthropic

    from noviello_funil.brain import gerar_followup_msg as gen
    from noviello_funil.brain import load_skill, triagem
    from noviello_funil.config import Settings
    from noviello_funil.db import connect, run_migrations

    settings = Settings()
    logging.basicConfig(level=settings.log_level)

    conn = connect(settings.database_path)
    run_migrations(conn)

    # C1 (auditoria 24/jun): sem JURICHAT_BOT_USER_ID o Signal 0 (detecção de
    # "humano assumiu") fica desligado e um restore/redeploy reintroduz o estado
    # quebrado em silêncio. Avisa alto no boot.
    if not settings.jurichat_bot_user_id:
        logger.warning(
            "JURICHAT_BOT_USER_ID VAZIO no .env — detecção de 'humano assumiu' "
            "(Signal 0) DESLIGADA; o bot pode atropelar atendimento humano. "
            "Configure (ver .env.example)."
        )

    jurichat = JurichatClient(
        api_key=settings.jurichat_api_key,
        base_url=settings.jurichat_base_url,
        bot_user_id=settings.jurichat_bot_user_id,
    )
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    # Google Calendar é opcional. Se as 3 creds OAuth não estão no .env,
    # o scheduler roda normal e ações de agendamento viram handoff
    # automático (msg "vou te conectar com o Mario").
    calendar_config: CalendarConfig | None = None
    if (
        settings.google_oauth_client_id
        and settings.google_oauth_client_secret
        and settings.google_oauth_refresh_token
    ):
        calendar_client = GoogleCalendarClient(
            client_id=settings.google_oauth_client_id,
            client_secret=settings.google_oauth_client_secret,
            refresh_token=settings.google_oauth_refresh_token,
            calendar_id=settings.google_calendar_id,
            timezone=settings.calendar_timezone,
        )
        calendar_config = CalendarConfig(
            client=calendar_client,
            business_hours_start=settings.calendar_business_hours_start,
            business_hours_end=settings.calendar_business_hours_end,
            slot_min=settings.calendar_slot_min,
            buffer_min=settings.calendar_buffer_min,
            lookahead_days=settings.calendar_lookahead_days,
            num_slots=settings.calendar_num_slots,
            morning_start=settings.calendar_morning_start,
            morning_end=settings.calendar_morning_end,
            timezone=settings.calendar_timezone,
        )
    else:
        calendar_client = None

    # Juridiq é opcional — sem chave, intake automático desligado.
    juridiq_client: JuridiqClient | None = None
    if settings.juridiq_api_key:
        juridiq_client = JuridiqClient(
            api_key=settings.juridiq_api_key,
            base_url=settings.juridiq_base_url,
        )
    # ZapSign no scheduler: SÓ pro sweep do pós-assinatura (#36). Sem a flag ou o
    # token, fica None e o sweep é no-op.
    zapsign_pos: ZapSignClient | None = None
    if settings.pos_assinatura_ativo and settings.zapsign_api_token:
        zapsign_pos = ZapSignClient(
            settings.zapsign_api_token, settings.zapsign_base_url,
        )
    # Multi-vertical prompt (imobiliário + sucessório + saúde). Substitui
    # o saude_suplementar.md anterior — vê src/noviello_funil/skills/.
    skill = load_skill("atendente_geral")

    bound_followup = partial(
        gen,
        client=anthropic_client,
        model=settings.anthropic_model_followup,
        skill_content=skill,
    )
    bound_triagem = partial(
        triagem,
        client=anthropic_client,
        model=settings.anthropic_model,
        skill_content=skill,
    )

    async def _full_cycle() -> None:
        # 1. Sync Jurichat conversations into our DB (registers new
        #    leads). Required because Jurichat has no per-message
        #    webhook event — we discover leads by polling.
        await sync_jurichat_conversations(
            get_db=lambda: conn,
            jurichat=jurichat,
            inbox_id=settings.jurichat_inbox_id,
            mario_conversation_id=settings.mario_conversation_id,
        )
        # 2. Poll cycle drives Claude on em_conversa leads (including
        #    the ones we just synced — they were scheduled for now).
        await run_poll_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            triagem_fn=bound_triagem,
            mario_conversation_id=settings.mario_conversation_id,
            max_turnos=settings.max_turnos_por_lead,
            calendar=calendar_config,
            bot_user_id=settings.jurichat_bot_user_id,
            juridiq=juridiq_client,
            datajud_api_key=settings.datajud_api_key,
        )
        # 2.5 (D4, 25/jun): detecta reuniões marcadas FORA do bot e vincula ao
        #    lead (pelo email do convidado) ANTES do reminder_cycle, pra elas já
        #    entrarem nos lembretes neste mesmo ciclo.
        if settings.calendar_sync_manual:
            await sync_reunioes_manuais(
                get_db=lambda: conn,
                calendar=calendar_config,
                jurichat=jurichat,
                mario_conversation_id=settings.mario_conversation_id,
            )
        # 2.6 (#36, 25/jun): sweep do pós-assinatura — retoma intake/PDF/tarefa
        #    de contratos ASSINADOS cujo passo falhou (o webhook signed não
        #    reentrega). Só com a flag ligada E ZapSign+Juridiq instanciados.
        if settings.pos_assinatura_ativo:
            await sweep_pos_assinatura(
                get_db=lambda: conn,
                zapsign=zapsign_pos,
                juridiq=juridiq_client,
                jurichat=jurichat,
                settings=settings,
            )
        # 3. Reminder cycle envia lembretes 24h/2h/30min antes de
        #    cada reunião agendada.
        await run_reminder_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            mario_conversation_id=settings.mario_conversation_id,
            calendar=calendar_config,
            base_url=settings.funil_base_url,
        )
        # 4. Follow-up cycle nudges idle leads.
        await run_followup_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            gerar_followup_msg=bound_followup,
            followup_2_apos_horas=settings.followup_2_apos_horas,
            encerramento_apos_horas=settings.encerramento_apos_horas,
            followup_1_apos_horas=settings.followup_1_apos_horas,
            bot_user_id=settings.jurichat_bot_user_id,
        )

    async def _full_cycle_with_cleanup() -> int:
        """Run cycle + close async clients within the SAME event loop.

        Using two separate `asyncio.run` calls (one for the cycle, one
        for aclose) corrupts the httpx async transport because the
        AsyncClient was opened on the first loop and the second loop
        rejects it as "Event loop is closed".
        """
        try:
            await _full_cycle()
            # Dead-man's switch: ping SÓ em ciclo bem-sucedido. Se o
            # serviço travar/crashar em loop, o healthchecks.io detecta
            # a ausência de pings e alerta Mario por email.
            await ping_healthcheck(settings.healthcheck_ping_url)
            return 0
        except Exception:
            logger.exception("scheduler cycle failed")
            return 1
        finally:
            await jurichat.aclose()
            if calendar_client is not None:
                await calendar_client.aclose()
            if juridiq_client is not None:
                await juridiq_client.aclose()
            if zapsign_pos is not None:
                await zapsign_pos.aclose()

    try:
        return asyncio.run(_full_cycle_with_cleanup())
    finally:
        conn.close()
