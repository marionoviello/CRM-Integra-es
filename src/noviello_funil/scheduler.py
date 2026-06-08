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
import hashlib
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from noviello_funil.brain import Decisao, DecisaoInvalida
from noviello_funil.outbound import (
    JurichatClient,
    format_notification,
    notify_mario,
)
from noviello_funil.state import (
    CLEAR_PROXIMA_ACAO,
    Estado,
    clear_next_action,
    list_leads_para_polling,
    list_leads_vencidos,
    mark_lead_activity_now,
    register_error,
    schedule_next_action_seconds,
    transicao,
    update_transcript_hash,
)

logger = logging.getLogger(__name__)

OPT_IN_TAGS = frozenset({"Fazer Follow up", "Proposta enviada"})

FOLLOWUP_2_TEXT = (
    "{nome}, percebi que talvez não seja o momento certo. "
    "Posso encerrar nosso atendimento por aqui? "
    "Se preferir continuar depois, é só me chamar novamente."
)

# Default polling cadence. Overridable via run_poll_cycle parameter.
DEFAULT_POLL_INTERVAL_SECONDS = 60


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


async def run_poll_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    triagem_fn: Callable[..., Awaitable[Decisao]],
    mario_conversation_id: str,
    max_turnos: int,
    poll_interval_seconds: int = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """Process all em_conversa leads whose poll tick is due."""
    conn = get_db()
    leads = list_leads_para_polling(conn)
    logger.info("poll tick: %d leads em_conversa due", len(leads))

    for lead in leads:
        lead_id = lead["id"]
        conv_id = lead["jurichat_conversation_id"]

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

        # Nothing new since the last poll → just reschedule.
        if new_hash == old_hash:
            schedule_next_action_seconds(conn, lead_id, poll_interval_seconds)
            continue

        # New content detected — mark lead as freshly active so the
        # follow-up cycle's "idle > 24h" carve-out keeps its hands off.
        mark_lead_activity_now(conn, lead_id)

        # Signal 1: Mario assumed the conversation (last line is his).
        if _last_line_from_atendente(transcript):
            transicao(
                conn, lead_id, Estado.AGUARDANDO_HUMANO,
                motivo="mensagem_mario_detectada",
                proxima_acao_horas=CLEAR_PROXIMA_ACAO,
            )
            update_transcript_hash(conn, lead_id, new_hash)
            continue

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
            # Send the closing message, then hand off.
            try:
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


async def run_followup_cycle(
    *,
    get_db: Callable[[], Any],
    jurichat: JurichatClient,
    gerar_followup_msg: Callable[..., Awaitable[str]],
    followup_2_apos_horas: int,
    encerramento_apos_horas: int,
) -> None:
    """Process all due leads in a single pass."""
    conn = get_db()
    vencidos = list_leads_vencidos(conn)
    logger.info("scheduler tick: %d leads vencidos", len(vencidos))

    for lead in vencidos:
        try:
            tags = await jurichat.get_lead_tags(lead["jurichat_lead_id"])
        except Exception as exc:
            logger.exception("get_lead_tags failed for %s: %s", lead["id"], exc)
            register_error(conn, lead["id"], "jurichat_get_tags_failed")
            continue

        if not is_eligible_for_followup(tags):
            register_error(conn, lead["id"], "excluido_followup_etiqueta")
            clear_next_action(conn, lead["id"])
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
    )
    anthropic_client = AsyncAnthropic(api_key=settings.anthropic_api_key)
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
        # Poll first so any state transitions land before the follow-up
        # cycle re-reads the lead table.
        await run_poll_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            triagem_fn=bound_triagem,
            mario_conversation_id=settings.mario_conversation_id,
            max_turnos=settings.max_turnos_por_lead,
        )
        await run_followup_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            gerar_followup_msg=bound_followup,
            followup_2_apos_horas=settings.followup_2_apos_horas,
            encerramento_apos_horas=settings.encerramento_apos_horas,
        )

    try:
        asyncio.run(_full_cycle())
        return 0
    except Exception:
        logger.exception("scheduler cycle failed")
        return 1
    finally:
        asyncio.run(jurichat.aclose())
        conn.close()
