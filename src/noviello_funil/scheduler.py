"""Follow-up scheduler — invoked hourly by systemd timer.

Reads leads with proxima_acao_em < now and dispatches the right
follow-up step based on their state machine position.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from noviello_funil.outbound import JurichatClient
from noviello_funil.state import (
    Estado,
    clear_next_action,
    list_leads_vencidos,
    register_error,
    schedule_next_action,
    transicao,
)

logger = logging.getLogger(__name__)

OPT_IN_TAGS = frozenset({"Fazer Follow up", "Proposta enviada"})

FOLLOWUP_2_TEXT = (
    "{nome}, percebi que talvez não seja o momento certo. "
    "Posso encerrar nosso atendimento por aqui? "
    "Se preferir continuar depois, é só me chamar novamente."
)


def is_eligible_for_followup(tags: list[str]) -> bool:
    """Strictly opt-in OR no-tag rule per spec §7.2.b."""
    if not tags:
        return True
    tag_set = set(tags)
    return bool(tag_set & OPT_IN_TAGS)


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
                await jurichat.send_message(
                    lead["jurichat_conversation_id"], texto,
                )
                transicao(
                    conn, lead["id"], Estado.FOLLOW_UP_1_ENVIADO,
                    motivo="scheduler_followup_1",
                )
                schedule_next_action(
                    conn, lead["id"], horas=followup_2_apos_horas,
                )

            elif estado == Estado.FOLLOW_UP_1_ENVIADO:
                nome = lead["contato_nome"] or "Olá"
                texto = FOLLOWUP_2_TEXT.format(nome=nome)
                await jurichat.send_message(
                    lead["jurichat_conversation_id"], texto,
                )
                transicao(
                    conn, lead["id"], Estado.FOLLOW_UP_2_ENVIADO,
                    motivo="scheduler_followup_2",
                )
                schedule_next_action(
                    conn, lead["id"], horas=encerramento_apos_horas,
                )

            elif estado == Estado.FOLLOW_UP_2_ENVIADO:
                # Silent close — no new message
                transicao(
                    conn, lead["id"], Estado.ENCERRADO_SEM_RESPOSTA,
                    motivo="scheduler_encerramento",
                )
                clear_next_action(conn, lead["id"])

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

    Reads settings, opens DB + client, runs one cycle, exits 0/1.
    """
    from functools import partial

    from anthropic import AsyncAnthropic

    from noviello_funil.brain import gerar_followup_msg as gen
    from noviello_funil.brain import load_skill
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
    skill = load_skill("saude_suplementar")

    bound_gen = partial(
        gen,
        client=anthropic_client,
        model=settings.anthropic_model,
        skill_content=skill,
    )

    try:
        asyncio.run(run_followup_cycle(
            get_db=lambda: conn,
            jurichat=jurichat,
            gerar_followup_msg=bound_gen,
            followup_2_apos_horas=settings.followup_2_apos_horas,
            encerramento_apos_horas=settings.encerramento_apos_horas,
        ))
        return 0
    except Exception:
        logger.exception("scheduler cycle failed")
        return 1
    finally:
        asyncio.run(jurichat.aclose())
        conn.close()
