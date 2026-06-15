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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.calendar_client import (
    GoogleCalendarClient,
    GoogleCalendarError,
    Slot,
)
from noviello_funil.conflito import checar_conflito
from noviello_funil.juridiq_client import JuridiqClient, intake_lead_agendado
from noviello_funil.opt_out import detectar_opt_out, registrar_opt_out
from noviello_funil.person_index import resolver_telefone
from noviello_funil.urgencia import detectar_urgencia
from noviello_funil.outbound import (
    JurichatClient,
    format_notification,
    notify_mario,
    split_conversation_ids,
)
from noviello_funil.state import (
    CLEAR_PROXIMA_ACAO,
    Estado,
    clear_reuniao,
    create_lead_if_absent,
    get_lead_by_conversation,
    list_leads_com_reuniao_futura,
    list_leads_para_polling,
    list_leads_para_reativacao,
    list_leads_vencidos,
    mark_cliente_checado,
    mark_lead_activity_now,
    mark_lembrete_enviado,
    mark_urgencia_alertada,
    register_error,
    schedule_next_action_seconds,
    set_reuniao,
    transicao,
    update_transcript_hash,
)

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


def _count_lead_lines(transcript: str) -> int:
    """Number of lines starting with ``Lead:`` (whitespace tolerated)."""
    return sum(
        1 for line in transcript.splitlines() if line.lstrip().startswith("Lead:")
    )


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
) -> None:
    """Busca slots reais, substitui ``{{HORARIOS}}``, envia."""
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

    try:
        slots = await calendar.client.find_available_slots(
            business_hours_start=calendar.business_hours_start,
            business_hours_end=calendar.business_hours_end,
            slot_min=calendar.slot_min,
            buffer_min=calendar.buffer_min,
            lookahead_days=calendar.lookahead_days,
            num_slots=calendar.num_slots,
        )
    except (GoogleCalendarError, Exception) as exc:
        logger.exception(
            "find_available_slots failed for lead=%s: %s", lead_id, exc,
        )
        register_error(conn, lead_id, "calendar_find_slots_failed")
        schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
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
        msg = (
            "Esse horário já passou! Me diz qual dos horários que te "
            "mandei funciona, ou me avisa que eu te mostro a agenda "
            "atualizada."
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

    # Lead JÁ TINHA reunião marcada e confirmou outra (remarcação que
    # o Claude rotulou de confirmar em vez de remarcar): cancela o
    # evento antigo antes de criar o novo — senão Mario fica com
    # double-booking e o lead com 2 convites (auditoria 2026-06-10).
    evento_antigo = lead["reuniao_event_id"]
    if evento_antigo:
        try:
            await calendar.client.cancel_event(evento_antigo)
            logger.info(
                "lead=%s: evento antigo %s cancelado antes do novo",
                lead_id, evento_antigo,
            )
        except Exception as exc:
            logger.warning(
                "cancel evento antigo %s falhou (lead=%s): %s — segue",
                evento_antigo, lead_id, exc,
            )

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
        # Degradação: avisa lead, notifica Mario, handoff manual.
        await _handoff_sem_calendar(
            conn=conn, lead=lead, transcript=transcript, new_hash=new_hash,
            jurichat=jurichat, mario_conversation_id=mario_conversation_id,
            motivo="falha_criar_evento_calendar",
        )
        return

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

    # Salva reunião no DB pro reminder_cycle (lembretes 24h/2h/30min).
    # Lead PERMANECE em_conversa pra bot poder processar resposta do
    # lead a um lembrete (ex: "preciso remarcar").
    # Persiste o ISO NORMALIZADO (aware, com offset) — nunca o cru do
    # LLM, que pode vir naive e quebrar o reminder_cycle 3h.
    iso_normalizado = start.isoformat()
    set_reuniao(
        conn, lead_id,
        reuniao_em_iso=iso_normalizado, event_id=event_id, meet_link=meet_link,
    )
    schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
    update_transcript_hash(conn, lead_id, new_hash)
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
    # novos (substitui {{HORARIOS}} na mensagem do Claude).
    await _handle_oferecer_horarios(
        conn=conn, lead=lead, decisao=decisao,
        transcript=transcript, new_hash=new_hash,
        jurichat=jurichat, calendar=calendar,
        mario_conversation_id=mario_conversation_id,
        poll_interval_seconds=poll_interval_seconds,
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


# Tags que indicam que a conversa NÃO deve ser atendida pelo bot
# (cliente já existente, advogado já lidando, lead desqualificado, etc.).
EXCLUDED_TAGS_FOR_BOT = frozenset({
    "Cliente Ativo",
    "Pagamento pendente",
    "Reunião marcada",
    "Advogado adverso",
    "Desqualificado",
})


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
        transicao(
            conn, lead["id"], Estado.EM_CONVERSA,
            motivo="lead_respondeu_reativacao",
        )
        schedule_next_action_seconds(conn, lead["id"], 0)

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

        # Signal 2: turn cap reached → hand off to Mario.
        if _count_lead_lines(transcript) >= max_turnos:
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
            # Guardrail (bug 2026-06-09): Claude usa "propor" pra leads
            # prontos pra fechar em vez de oferecer agendamento direto.
            # Se temos calendar configurado, REDIRECIONAMOS pra fluxo
            # de agendamento: se já tem email na transcrição, oferece
            # horários direto; senão pede email primeiro.
            if calendar is not None and calendar.client is not None:
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


async def run_reminder_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
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
            logger.warning(
                "lead=%s reuniao_em inválido %r: %s",
                lead["id"], lead["reuniao_em"], exc,
            )
            continue

        delta = reuniao_dt - now

        # Reunião já passou — limpa.
        if delta.total_seconds() < 0:
            logger.info("lead=%s reuniao passou, limpando", lead["id"])
            clear_reuniao(conn, lead["id"])
            continue

        meet_link = lead["reuniao_meet_link"] or ""
        nome = lead["contato_nome"] or "Olá"
        conv_id = lead["jurichat_conversation_id"]
        horario_human = _format_reuniao_human(reuniao_dt)

        # Decide quais lembretes disparar neste tick. Ordem importa:
        # se faltam 5 min e nenhum foi enviado, manda APENAS o 30min
        # (não faria sentido mandar "24h" agora).
        if delta <= datetime.timedelta(minutes=30):
            if lead["lembrete_30min_enviado_em"] is None:
                msg = _msg_lembrete_30min(nome, horario_human, meet_link)
                if await _enviar_lembrete(
                    jurichat, conv_id, msg, lead["id"], "30min",
                ):
                    mark_lembrete_enviado(conn, lead["id"], "30min")
        elif delta <= datetime.timedelta(hours=2):
            if lead["lembrete_2h_enviado_em"] is None:
                msg = _msg_lembrete_2h(nome, horario_human, meet_link)
                if await _enviar_lembrete(
                    jurichat, conv_id, msg, lead["id"], "2h",
                ):
                    mark_lembrete_enviado(conn, lead["id"], "2h")
        elif delta <= datetime.timedelta(hours=24):
            if lead["lembrete_24h_enviado_em"] is None:
                msg = _msg_lembrete_24h(nome, horario_human, meet_link)
                if await _enviar_lembrete(
                    jurichat, conv_id, msg, lead["id"], "24h",
                ):
                    mark_lembrete_enviado(conn, lead["id"], "24h")


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
        f"com o Mario."
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
        f"com o Mario."
    )
    if meet_link:
        base += f"\n\nLink Meet: {meet_link}"
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


async def run_followup_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    gerar_followup_msg: Callable[..., Awaitable[str]],
    followup_2_apos_horas: int,
    encerramento_apos_horas: int,
    followup_1_apos_horas: int = 48,
) -> None:
    """Process all due leads in a single pass."""
    conn = get_db()
    vencidos = list_leads_vencidos(conn, fu1_apos_horas=followup_1_apos_horas)
    logger.info("scheduler tick: %d leads vencidos", len(vencidos))

    for lead in vencidos:
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
    # Multi-vertical prompt (imobiliário + sucessório + saúde). Substitui
    # o saude_suplementar.md anterior — vê src/noviello_funil/skills/.
    skill = load_skill("atendente_geral")

    bound_followup = partial(
        gen,
        client=anthropic_client,
        model=settings.anthropic_model,
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
        )
        # 3. Reminder cycle envia lembretes 24h/2h/30min antes de
        #    cada reunião agendada.
        await run_reminder_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
        )
        # 4. Follow-up cycle nudges idle leads.
        await run_followup_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            gerar_followup_msg=bound_followup,
            followup_2_apos_horas=settings.followup_2_apos_horas,
            encerramento_apos_horas=settings.encerramento_apos_horas,
            followup_1_apos_horas=settings.followup_1_apos_horas,
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

    try:
        return asyncio.run(_full_cycle_with_cleanup())
    finally:
        conn.close()
